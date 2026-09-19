import os
import io
import base64
import hashlib
import locale
import json
import re
import folder_paths
import time
import numpy
import PIL.Image
import torch
import torch.nn.functional as F
import requests
import cv2
import asyncio
import threading
from volcenginesdkarkruntime import Ark

from comfy_api.latest import io as comfy_io

import logging

from .constants import (
    LOG_TRANSLATIONS,
    ERROR_TEXT_MATCH_RULES,
    JIMENG_API_BASE_URL,
    AGENTPLAN_API_BASE_URL,
    AUTH_MODE_ARK,
    AUTH_MODE_AGENTPLAN,
    AUTH_MODE_OPTIONS,
)
from .models_config import is_agentplan_supported_visual_model

LOG_PREFIX = "[JimengAI] "

def patch_log_translations():
    """
    自动为日志消息添加前缀。
    """
    ignore_keys = {"api_errors"}
    
    for lang in LOG_TRANSLATIONS:
        trans_map = LOG_TRANSLATIONS[lang]
        for key, value in trans_map.items():
            if key in ignore_keys or key.startswith("est_"):
                continue
            
            if isinstance(value, str):
                if value.strip().startswith("-"):
                    continue
                
                if value.startswith("\n"):
                    if LOG_PREFIX.strip() not in value:
                        trans_map[key] = "\n" + LOG_PREFIX + value[1:]
                else:
                    if not value.startswith(LOG_PREFIX):
                        trans_map[key] = LOG_PREFIX + value

patch_log_translations()

logger = logging.getLogger("JimengAI")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

GLOBAL_CATEGORY = "JimengAI"

jimeng_api_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_KEYS_FILE = os.path.join(jimeng_api_dir, "api_keys.json")
FILES_UPLOAD_CACHE_FILE = os.path.join(jimeng_api_dir, "files_upload_cache.json")

JimengClientType = comfy_io.Custom("JIMENG_CLIENT")


def detect_system_language():
    """
    检测系统语言，如果是中文环境则返回 'zh'，否则返回 'en'。
    """
    try:
        lang_code, _ = locale.getdefaultlocale()
        if lang_code and lang_code.startswith("zh"):
            return "zh"
    except:
        pass
    return "en"


class LocalizationState:
    def __init__(self, default_lang="en"):
        self._lock = threading.RLock()
        self._lang = "en"
        self.set_language(default_lang)

    def set_language(self, lang):
        normalized_lang = lang if lang in LOG_TRANSLATIONS else "en"
        with self._lock:
            self._lang = normalized_lang

    def refresh_from_system(self):
        self.set_language(detect_system_language())

    def get_language(self):
        with self._lock:
            return self._lang

    def get_mapping(self):
        return LOG_TRANSLATIONS.get(self.get_language(), LOG_TRANSLATIONS["en"])


LOCALIZATION_STATE = LocalizationState(detect_system_language())


def normalize_auth_mode(auth_mode):
    """
    归一化鉴权模式；未知值一律回退为普通方舟 API 模式。
    """
    return AUTH_MODE_AGENTPLAN if str(auth_mode) == AUTH_MODE_AGENTPLAN else AUTH_MODE_ARK


def resolve_base_url(auth_mode):
    """
    根据鉴权模式返回对应的 Base URL。
    AgentPlan 需使用专属 Base URL（包含 /plan），普通方舟 API Key 与
    AgentPlan 专属 API Key 不互通。
    """
    if normalize_auth_mode(auth_mode) == AUTH_MODE_AGENTPLAN:
        return AGENTPLAN_API_BASE_URL
    return JIMENG_API_BASE_URL


def auth_mode_display_name(auth_mode):
    """
    获取鉴权模式的本地化显示名称（去除日志前缀，避免嵌套）。
    """
    if normalize_auth_mode(auth_mode) == AUTH_MODE_AGENTPLAN:
        display_name = get_text("auth_mode_name_agentplan")
    else:
        display_name = get_text("auth_mode_name_ark")
    return display_name.replace(LOG_PREFIX, "")


class ApiKeyStore:
    def __init__(self, config_file):
        self.config_file = config_file
        self._lock = threading.RLock()
        self._items = []

    def load(self):
        loaded_items = []
        LOCALIZATION_STATE.refresh_from_system()

        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, "r", encoding="utf-8") as f:
                    keys_data = json.load(f)
                if isinstance(keys_data, list):
                    for item in keys_data:
                        if "customName" in item and "apiKey" in item:
                            entry = dict(item)
                            raw_mode = entry.get("authMode")
                            # 旧版本配置无 authMode，保留 None 以便节点选择生效
                            entry["authMode"] = (
                                raw_mode
                                if raw_mode in (AUTH_MODE_ARK, AUTH_MODE_AGENTPLAN)
                                else None
                            )
                            loaded_items.append(entry)
        except Exception as e:
            log_msg("api_load_error", e=e)

        with self._lock:
            self._items = loaded_items

    def save(self):
        with self._lock:
            serializable_items = list(self._items)
        try:
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(serializable_items, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save API key: {e}")
            return False
        return True

    def upsert(self, name, key, auth_mode=AUTH_MODE_ARK):
        auth_mode = normalize_auth_mode(auth_mode)
        with self._lock:
            updated = False
            for item in self._items:
                if item["customName"] == name:
                    item["apiKey"] = key
                    item["authMode"] = auth_mode
                    updated = True
                    break

            if not updated:
                self._items.append(
                    {
                        "customName": name,
                        "apiKey": key,
                        "authMode": auth_mode,
                    }
                )

        return self.save()

    def get_items(self):
        with self._lock:
            return [dict(item) for item in self._items]

    def get_key_names(self):
        with self._lock:
            return [item["customName"] for item in self._items]

    def find_entry(self, key_name):
        with self._lock:
            for item in self._items:
                if item["customName"] == key_name:
                    return dict(item)
        return None
    def find_api_key(self, key_name):
        entry = self.find_entry(key_name)
        return entry["apiKey"] if entry else None


API_KEY_STORE = ApiKeyStore(API_KEYS_FILE)


def get_text(key, **kwargs):
    """
    获取指定 key 的本地化文本。
    """
    mapping = LOCALIZATION_STATE.get_mapping()
    msg = mapping.get(key, LOG_TRANSLATIONS["en"].get(key, key))
    if kwargs:
        try:
            return msg.format(**kwargs)
        except:
            pass
    return msg


def log_msg(key, default_msg="", **kwargs):
    """
    记录本地化日志信息。
    """
    mapping = LOCALIZATION_STATE.get_mapping()
    msg = mapping.get(key, None)
    if not msg:
        msg = LOG_TRANSLATIONS["en"].get(key, default_msg)
    if msg:
        raw_api_response = kwargs.pop("raw_api_response", None)
        rendered_msg = msg
        try:
            rendered_msg = msg.format(**kwargs)
        except:
            pass
        logger.info(rendered_msg)
        if any(code in str(rendered_msg) for code in ("InvalidParameter", "MissingParameter")):
            if raw_api_response is None:
                raw_api_response = kwargs.get("e")
            if raw_api_response is None:
                raw_api_response = kwargs.get("msg")
            if raw_api_response is not None:
                logger.info(f"{LOG_PREFIX}Raw API response: {raw_api_response}")


def get_node_count_in_workflow(class_type, prompt=None):
    """
    获取当前工作流中指定类型节点的数量。
    """
    try:
        if prompt is None:
            from server import PromptServer
            prompt = PromptServer.instance.prompt
        
        if not prompt:
            return 0
        
        count = 0
        for key, value in prompt.items():
            if value.get("class_type") == class_type:
                count += 1
        return count
    except Exception:
        return 0


def format_api_error(e):
    """
    格式化 API 错误信息。
    尝试解析错误代码并返回对应的本地化错误描述。
    """
    mapping = LOCALIZATION_STATE.get_mapping()
    error_map = mapping.get("api_errors", {})
    fallback_map = LOG_TRANSLATIONS["en"].get("api_errors", {})

    err_code = None
    err_msg = str(e)
    detected_code = None

    code_match = re.search(r"'code':\s*'([^']+)'", err_msg)
    if code_match:
        err_code = code_match.group(1)

    msg_match = re.search(r"'message':\s*'([^']+)'", err_msg)
    if msg_match:
        extracted_msg = msg_match.group(1)
        if len(extracted_msg) > 0:
            err_msg = extracted_msg

    for keyword, mapped_code in ERROR_TEXT_MATCH_RULES.items():
        if keyword.lower() in err_msg.lower():
            detected_code = mapped_code
            break

    final_code = detected_code if detected_code else err_code

    if final_code:
        final_code = str(final_code)
        matched_msg = None

        if final_code in error_map:
            matched_msg = error_map[final_code]
        elif final_code in fallback_map:
            matched_msg = fallback_map[final_code]

        if not matched_msg:
            for key in error_map:
                if final_code.startswith(key):
                    matched_msg = error_map[key]
                    break
            if not matched_msg:
                for key in fallback_map:
                    if final_code.startswith(key):
                        matched_msg = fallback_map[key]
                        break

        if matched_msg:
            if "%s" in matched_msg:
                def _extract_account_model(text: str):
                    if not text:
                        return None, None
                    m = re.search(
                        r"account\s*\[([^\]]+)\].*?\[([^\]]+)\]\s*model",
                        text,
                        flags=re.IGNORECASE | re.DOTALL,
                    )
                    if m:
                        return m.group(1).strip(), m.group(2).strip()
                    m = re.search(
                        r"your\s+account\s+([^\s]+).*?model\s+([^\s\.\]]+)",
                        text,
                        flags=re.IGNORECASE | re.DOTALL,
                    )
                    if m:
                        return m.group(1).strip(), m.group(2).strip()
                    return None, None

                account, model = _extract_account_model(err_msg)
                if account and model:
                    matched_msg = matched_msg % (account, model)
            return f"{LOG_PREFIX}{matched_msg} (Code: {final_code})"

    return f"{LOG_PREFIX}Error: {err_msg}"


def load_api_keys():
    """
    加载 API 密钥配置文件 (api_keys.json)。
    """
    API_KEY_STORE.load()


def save_api_key(name, key, auth_mode=AUTH_MODE_ARK):
    """
    保存新的 API Key 到配置文件（连同鉴权模式）。
    """
    if API_KEY_STORE.upsert(name, key, auth_mode=auth_mode):
        logger.info(f"Saved API Key: {name}")


def validate_api_key(api_key: str, base_url: str = JIMENG_API_BASE_URL) -> bool:
    """
    验证 API Key 是否有效。
    需传入与该 Key 匹配的 Base URL（普通方舟 / AgentPlan 专属）。
    """
    try:
        url = base_url
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

        response = requests.get(url, headers=headers, timeout=10)

        if response.status_code == 401:
            return False

        return True

    except Exception as e:
        logger.error(f"API Key validation error: {e}")
        return False


def _normalize_expire_seconds(expire_seconds):
    expire_seconds = int(expire_seconds if expire_seconds is not None else 604800)
    return max(86400, min(expire_seconds, 2592000))


def _normalize_cache_key(cache_key):
    if isinstance(cache_key, str):
        try:
            cache_key = json.loads(cache_key)
        except Exception:
            return None
    if isinstance(cache_key, list):
        cache_key = tuple(cache_key)
    if not isinstance(cache_key, tuple):
        return None
    if len(cache_key) == 2:
        file_path, fps = cache_key
        expire_seconds = 604800
    elif len(cache_key) == 3:
        file_path, fps, expire_seconds = cache_key
    else:
        return None
    if not isinstance(file_path, str) or not file_path:
        return None
    normalized_fps = float(fps) if fps is not None else None
    normalized_expire_seconds = _normalize_expire_seconds(expire_seconds)
    return (file_path, normalized_fps, normalized_expire_seconds)


def _serialize_cache_key(cache_key):
    return json.dumps(list(cache_key), ensure_ascii=False)


def _compute_file_sha256(file_path):
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            if chunk:
                hasher.update(chunk)
    return hasher.hexdigest()


class UploadCacheStore:
    def __init__(self, cache_file):
        self.cache_file = cache_file
        self._lock = threading.RLock()
        self._data = {}

    def load(self):
        loaded_data = {}
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    cache_data = json.load(f)
                if isinstance(cache_data, dict):
                    now_ts = int(time.time())
                    for raw_key, entry in cache_data.items():
                        cache_key = _normalize_cache_key(raw_key)
                        if cache_key is None or not isinstance(entry, dict):
                            continue
                        file_id = entry.get("file_id")
                        expire_at = int(entry.get("expire_at", 0) or 0)
                        if file_id and expire_at > now_ts:
                            loaded_data[cache_key] = {
                                "file_id": file_id,
                                "expire_at": expire_at
                            }
            except Exception as e:
                logger.error(f"Failed to load upload cache: {e}")
        with self._lock:
            self._data = loaded_data

    def save(self):
        now_ts = int(time.time())
        serializable_data = {}
        with self._lock:
            for raw_key, entry in self._data.items():
                cache_key = _normalize_cache_key(raw_key)
                if cache_key is None or not isinstance(entry, dict):
                    continue
                file_id = entry.get("file_id")
                expire_at = int(entry.get("expire_at", 0) or 0)
                if file_id and expire_at > now_ts:
                    serializable_data[_serialize_cache_key(cache_key)] = {
                        "file_id": file_id,
                        "expire_at": expire_at
                    }
        try:
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(serializable_data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save upload cache: {e}")

    def get(self, cache_key):
        with self._lock:
            return self._data.get(cache_key)

    def set(self, cache_key, entry):
        with self._lock:
            self._data[cache_key] = entry

    def pop(self, cache_key, default=None):
        with self._lock:
            return self._data.pop(cache_key, default)

    def contains(self, cache_key):
        with self._lock:
            return cache_key in self._data

    def keys(self):
        with self._lock:
            return list(self._data.keys())


UPLOAD_CACHE_STORE = UploadCacheStore(FILES_UPLOAD_CACHE_FILE)


def load_files_upload_cache():
    UPLOAD_CACHE_STORE.load()


def save_files_upload_cache():
    UPLOAD_CACHE_STORE.save()


load_files_upload_cache()


async def upload_file_to_ark(client, file_path, fps=None, expire_seconds=604800, return_meta=False):
    """
    使用 client.ark.files.create 上传文件。
    """
    expire_seconds = _normalize_expire_seconds(expire_seconds)
    if fps is None:
        try:
            file_identity = f"sha256:{await asyncio.to_thread(_compute_file_sha256, file_path)}"
        except Exception:
            file_identity = file_path
    else:
        file_identity = file_path
    cache_key = (file_identity, float(fps) if fps is not None else None, expire_seconds)
    cache_fps = float(fps) if fps is not None else None
    now_ts = int(time.time())
    expire_at = now_ts + expire_seconds

    cached_entry = UPLOAD_CACHE_STORE.get(cache_key)
    legacy_path_key = None
    matched_cache_key = cache_key
    if cached_entry is None and fps is None and file_identity != file_path:
        legacy_path_key = (file_path, None, expire_seconds)
        cached_entry = UPLOAD_CACHE_STORE.get(legacy_path_key)
        if cached_entry is not None:
            matched_cache_key = legacy_path_key
    if isinstance(cached_entry, dict):
        cached_file_id = cached_entry.get("file_id")
        cached_expire_at = int(cached_entry.get("expire_at", 0) or 0)
        if cached_file_id:
            try:
                remote_file_info = await asyncio.to_thread(
                    client.ark.files.retrieve,
                    file_id=cached_file_id
                )
                remote_status = getattr(remote_file_info, "status", "unknown")
                remote_expire_at = int(getattr(remote_file_info, "expire_at", 0) or 0)
                if remote_status == "active" and remote_expire_at > now_ts:
                    cached_entry["expire_at"] = remote_expire_at
                    if legacy_path_key is not None and UPLOAD_CACHE_STORE.contains(legacy_path_key):
                        UPLOAD_CACHE_STORE.pop(legacy_path_key, None)
                        UPLOAD_CACHE_STORE.set(cache_key, cached_entry)
                    save_files_upload_cache()
                    log_msg("visual_found_file", path=file_path)
                    if return_meta:
                        return {
                            "file_id": cached_file_id,
                            "expire_at": remote_expire_at
                        }
                    return cached_file_id
            except Exception:
                if cached_expire_at > now_ts:
                    if legacy_path_key is not None and UPLOAD_CACHE_STORE.contains(legacy_path_key):
                        UPLOAD_CACHE_STORE.pop(legacy_path_key, None)
                        UPLOAD_CACHE_STORE.set(cache_key, cached_entry)
                        save_files_upload_cache()
                    log_msg("visual_found_file", path=file_path)
                    if return_meta:
                        return {
                            "file_id": cached_file_id,
                            "expire_at": cached_expire_at
                        }
                    return cached_file_id
        UPLOAD_CACHE_STORE.pop(matched_cache_key, None)
        save_files_upload_cache()
    elif cached_entry is not None:
        UPLOAD_CACHE_STORE.pop(cache_key, None)
        save_files_upload_cache()

    identity_candidates = {file_identity}
    if fps is None and file_identity != file_path:
        identity_candidates.add(file_path)
    stale_keys = []
    for existing_key in UPLOAD_CACHE_STORE.keys():
        normalized_existing_key = _normalize_cache_key(existing_key)
        if normalized_existing_key is None:
            continue
        existing_identity, existing_fps, existing_expire_seconds = normalized_existing_key
        if (
            existing_identity in identity_candidates
            and existing_fps == cache_fps
            and existing_expire_seconds != expire_seconds
        ):
            stale_keys.append(existing_key)

    if stale_keys and hasattr(client.ark, "files"):
        cache_changed = False
        for stale_key in stale_keys:
            stale_entry = UPLOAD_CACHE_STORE.get(stale_key)
            if isinstance(stale_entry, dict):
                stale_file_id = stale_entry.get("file_id")
                if stale_file_id:
                    try:
                        await asyncio.to_thread(
                            client.ark.files.delete,
                            file_id=stale_file_id
                        )
                    except Exception as e:
                        logger.error(f"Delete stale file failed for {stale_file_id}: {e}")
            UPLOAD_CACHE_STORE.pop(stale_key, None)
            cache_changed = True
        if cache_changed:
            save_files_upload_cache()
        
    try:
        log_msg("visual_uploading", path=file_path)
        if hasattr(client.ark, "files"):
            with open(file_path, "rb") as f:
                
                upload_kwargs = {
                    "file": f,
                    "purpose": "user_data",
                    "expire_at": expire_at
                }
                
                if fps is not None:
                    preprocess_config = {
                        "video": {
                            "fps": float(fps)
                        }
                    }
                    upload_kwargs["preprocess_configs"] = preprocess_config

                file_obj = await asyncio.to_thread(
                    client.ark.files.create,
                    **upload_kwargs
                )
                
                if hasattr(file_obj, "id"):
                    file_id = file_obj.id
                    log_msg("visual_uploaded", id=file_id, status=getattr(file_obj, 'status', 'unknown'))
                    
                    await wait_for_file_active(client, file_id)
                    
                    UPLOAD_CACHE_STORE.set(cache_key, {
                        "file_id": file_id,
                        "expire_at": expire_at
                    })
                    save_files_upload_cache()
                    if return_meta:
                        return {
                            "file_id": file_id,
                            "expire_at": expire_at
                        }
                    return file_id
                else:
                    raise JimengException("Upload failed: No file ID returned.")
        else:
             raise JimengException("SDK does not support files.create.")
             
    except Exception as e:
        logger.error(f"Upload failed for {file_path}: {e}")
        raise JimengException(f"File upload failed: {e}")


async def wait_for_file_active(client, file_id):
    """
    轮询文件状态，直到其变为“active”状态。
    """
    log_msg("visual_wait_active", id=file_id)
    
    while True:
        try:
            file_info = await asyncio.to_thread(
                client.ark.files.retrieve,
                file_id=file_id
            )
            
            status = getattr(file_info, "status", "unknown")
            log_msg("visual_file_status", id=file_id, status=status)
            
            if status == "active":
                return True
            elif status == "error":
                raise JimengException(f"File processing failed: {file_id}")
            
            await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Error checking file status: {e}")
            await asyncio.sleep(1)


def _tensor2images(tensor: torch.Tensor) -> list:
    """
    将 PyTorch Tensor 转换为 PIL Image 列表。
    """
    np_imgs = numpy.clip(tensor.cpu().numpy() * 255.0, 0, 255.0).astype(numpy.uint8)
    return [PIL.Image.fromarray(np_img) for np_img in np_imgs]


def _image_to_base64(image: torch.Tensor) -> str:
    """
    将单张图片 Tensor 转换为 Base64 编码字符串 (JPEG 格式)。
    """
    if image is None:
        return None
    with io.BytesIO() as bytes_io:
        _tensor2images(image)[0].save(bytes_io, format="JPEG")
        data_bytes = bytes_io.getvalue()
    return base64.b64encode(data_bytes).decode("utf-8")


def create_white_image_tensor(width=1024, height=1024):
    """
    创建一个指定大小的纯白图片 Tensor。
    Shape: [1, height, width, 3]
    """
    return torch.ones((1, height, width, 3), dtype=torch.float32)


def safe_cat_tensors(tensors, dim=0):
    """
    安全地将多个 Tensor 拼接在一起。
    主要用于处理 Adaptive Size 返回不同尺寸图片的情况。
    """
    if not tensors:
        return None

    if not isinstance(tensors, list):
        return tensors

    if len(tensors) == 0:
        return None

    target_tensor = tensors[0]
    target_h, target_w = target_tensor.shape[1], target_tensor.shape[2]

    processed_tensors = []
    for t in tensors:
        if t.shape[1] != target_h or t.shape[2] != target_w:
            t_permuted = t.permute(0, 3, 1, 2)
            t_resized = F.interpolate(
                t_permuted, size=(target_h, target_w), mode="bilinear", align_corners=False
            )
            t_final = t_resized.permute(0, 2, 3, 1)
            processed_tensors.append(t_final)
        else:
            processed_tensors.append(t)

    return torch.cat(processed_tensors, dim=dim)


def create_white_video_file(filename_prefix, width=1024, height=1024):
    """
    创建一个指定大小的 1 帧纯白视频文件 (H.264 MP4)。
    返回视频文件的绝对路径。
    """
    import tempfile
    try:
        import cv2
    except ImportError:
        return None

    try:
        temp_dir = folder_paths.get_temp_directory()
        timestamp = int(time.time() * 1000)
        
        flat_prefix = filename_prefix.replace("/", "_").replace("\\", "_")
        filename = f"{flat_prefix}_dummy_{timestamp}.mp4"
        
        filepath = os.path.join(temp_dir, filename)

        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        fps = 24.0
        out = cv2.VideoWriter(filepath, fourcc, fps, (width, height))
        
        if not out.isOpened():
            log_msg("err_create_dummy_video", e="Failed to open VideoWriter with mp4v")
            return None

        white_frame = numpy.ones((height, width, 3), dtype=numpy.uint8) * 255
        
        out.write(white_frame)
        out.release()
        
        if not os.path.exists(filepath):
             log_msg("err_create_dummy_video", e="File was not created on disk")
             return None
        
        return filepath
    except Exception as e:
        log_msg("err_create_dummy_video", e=e)
        return None


class JimengException(Exception):
    """
    Jimeng 自定义异常类。
    设置 jimeng_suppress_traceback = True 以在打印时抑制堆栈跟踪。
    """
    def __init__(self, message):
        super().__init__(message)
        self.jimeng_suppress_traceback = True


class JimengClients:
    """
    包装 Ark 客户端的容器类。
    """
    def __init__(self, ark_client, api_key=None, auth_mode=AUTH_MODE_ARK):
        self.ark = ark_client
        self.api_key = api_key
        self.auth_mode = normalize_auth_mode(auth_mode)
        self.base_url = resolve_base_url(self.auth_mode)

    def check_quota(self, model: str, estimated_cost: int):
        if self.auth_mode == AUTH_MODE_AGENTPLAN:
            if not is_agentplan_supported_visual_model(model):
                log_msg("warn_agentplan_model_unsupported", model=model)
        if not self.api_key:
            return
        from .quota import QuotaManager
        QuotaManager.instance().check_quota(self.api_key, model, estimated_cost)

    def update_usage(self, model: str, actual_cost: int):
        if not self.api_key:
            return
        from .quota import QuotaManager
        QuotaManager.instance().update_usage(self.api_key, model, actual_cost)


class JimengAPIClient(comfy_io.ComfyNode):
    """
    Jimeng API 客户端节点。
    负责加载 API 密钥并初始化 Ark 客户端。
    支持两种鉴权模式：
    - ark: 普通火山方舟 API（按量后付费）
    - agentplan: 方舟 AgentPlan 套餐（专属 Base URL + 专属 API Key，抵扣套餐额度）
    """
    @classmethod
    def define_schema(cls) -> comfy_io.Schema:
        load_api_keys()
        key_names = API_KEY_STORE.get_key_names()
        key_names.append("Custom")

        return comfy_io.Schema(
            node_id="JimengAPIClient",
            display_name="Jimeng API Client",
            category=GLOBAL_CATEGORY,
            inputs=[
                comfy_io.String.Input("new_api_key", default=""),
                comfy_io.String.Input("new_key_name", default=""),
                comfy_io.Combo.Input("key_name", options=key_names),
                comfy_io.Combo.Input(
                    "auth_mode", options=AUTH_MODE_OPTIONS, default=AUTH_MODE_ARK
                ),
            ],
            outputs=[JimengClientType.Output(display_name="client")],
        )

    @classmethod
    def execute(
        cls, key_name, new_api_key="", new_key_name="", auth_mode=AUTH_MODE_ARK
    ) -> comfy_io.NodeOutput:
        api_key = None
        selected_auth_mode = normalize_auth_mode(auth_mode)

        if key_name == "Custom":
            if not new_api_key or not new_api_key.strip():
                raise JimengException(get_text("err_new_key_empty"))

            api_key = new_api_key.strip()
            effective_auth_mode = selected_auth_mode

            if not validate_api_key(api_key, resolve_base_url(effective_auth_mode)):
                raise JimengException(get_text("err_new_key_invalid"))

            if new_key_name and new_key_name.strip():
                save_api_key(new_key_name.strip(), api_key, effective_auth_mode)
                print(get_text("info_new_key_saved", name=new_key_name.strip()))

        else:
            entry = API_KEY_STORE.find_entry(key_name)
            api_key = entry["apiKey"] if entry else None

            if entry and entry.get("authMode"):
                effective_auth_mode = normalize_auth_mode(entry["authMode"])
                if effective_auth_mode != selected_auth_mode:
                    log_msg(
                        "warn_auth_mode_conflict",
                        name=key_name,
                        stored=effective_auth_mode,
                        selected=selected_auth_mode,
                    )
            else:
                effective_auth_mode = selected_auth_mode

        if not api_key:
            log_msg("api_key_not_found", key_name=key_name)
            raise JimengException(get_text("popup_key_valid_err").format(key=key_name))

        base_url = resolve_base_url(effective_auth_mode)
        log_msg(
            "auth_mode_active",
            mode=auth_mode_display_name(effective_auth_mode),
            base_url=base_url,
        )

        ark_client = Ark(
            api_key=api_key, base_url=base_url
        )

        return comfy_io.NodeOutput(
            JimengClients(ark_client, api_key, effective_auth_mode)
        )
