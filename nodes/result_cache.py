"""
生成结果持久化缓存。

当「生成参数 + 参考素材内容」与历史成功任务一致时，复用已下载的本地
结果文件并跳过方舟任务提交，避免相同输入重复计费。

设计约束：
- 缓存 key 只由稳定、归一化的成分组成：模型版本、归一化 prompt、实际
  seed、resolution、aspect ratio、duration、generate_audio、
  enable_web_search 等请求参数，以及参考图/视频/音频的稳定内容哈希。
  路径、mtime、内存对象、UNIQUE_ID 等不稳定成分一律不进入 key。
- 命中时使用缓存持有的本地结果文件重建输出，绝不缓存 24 小时临时
  签名 URL。
- TTL 与容量上限参照上传缓存约定（默认 24 小时 / 256 条）。
- 写入采用「先复制文件、后原子写索引」，避免半成品命中；任何缓存
  故障（读写、清理、损坏）都安全降级为正常生成，不影响主流程。
"""

import hashlib
import json
import logging
import os
import shutil
import threading
import time
import uuid

logger = logging.getLogger("JimengAI")

RESULT_CACHE_DIR_NAME = "result_cache"
RESULT_CACHE_FILES_DIR_NAME = "files"
RESULT_CACHE_INDEX_NAME = "index.json"
RESULT_CACHE_TTL_SECONDS = 86400
RESULT_CACHE_MAX_ENTRIES = 256
RESULT_CACHE_ENTRY_VERSION = 1

RESULT_CACHE_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    RESULT_CACHE_DIR_NAME,
)

_result_cache_store = None
_result_cache_store_lock = threading.Lock()


def get_result_cache_store():
    """返回插件目录下结果缓存的单例 store；测试可替换该工厂。"""
    global _result_cache_store
    if _result_cache_store is None:
        with _result_cache_store_lock:
            if _result_cache_store is None:
                _result_cache_store = ResultCacheStore(RESULT_CACHE_ROOT)
    return _result_cache_store


def normalize_prompt(prompt):
    """归一化 prompt：仅去除首尾空白，保持语义内容不变。"""
    return str(prompt or "").strip()


def _canonical_json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _hash_file_content(path):
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            if chunk:
                hasher.update(chunk)
    return hasher.hexdigest()


def stable_video_hash(video):
    """
    计算参考视频的稳定内容哈希。

    仅基于文件/缓冲区内容，不使用路径、mtime 或内存对象地址；无法
    获取稳定内容时返回 None（调用方应跳过缓存）。
    """
    if video is None:
        return None
    get_stream_source = getattr(video, "get_stream_source", None)
    if not callable(get_stream_source):
        return None
    try:
        stream_source = get_stream_source()
    except Exception:
        return None
    if isinstance(stream_source, str):
        if not os.path.isfile(stream_source):
            return None
        try:
            return _hash_file_content(stream_source)
        except Exception:
            return None
    if hasattr(stream_source, "getbuffer"):
        try:
            return _sha256_bytes(bytes(stream_source.getbuffer()))
        except Exception:
            return None
    if hasattr(stream_source, "getvalue"):
        try:
            return _sha256_bytes(stream_source.getvalue())
        except Exception:
            return None
    return None


def _content_descriptor(content, video_hashes=None):
    """
    将提交给方舟的 content 转换为稳定描述。

    - 图片/音频条目本身即由内容派生（data URI），直接哈希。
    - 参考视频以 URL 提交，URL 不稳定，须使用预计算的内容哈希替换；
      无法一一对应时返回 None，表示无法构建稳定 key。
    """
    video_hashes = list(video_hashes or [])
    video_hash_index = 0
    descriptors = []
    if content is None:
        return []
    if not isinstance(content, list):
        return None
    if content and isinstance(content[0], list):
        return None
    for item in content:
        if not isinstance(item, dict):
            return None
        item_type = str(item.get("type", ""))
        if item_type == "text":
            descriptors.append(
                {
                    "type": "text",
                    "hash": _sha256_bytes(str(item.get("text", "")).encode("utf-8")),
                }
            )
        elif item_type == "image_url":
            url = str((item.get("image_url") or {}).get("url", ""))
            descriptors.append({"type": "image", "hash": _sha256_bytes(url.encode("utf-8"))})
        elif item_type == "audio_url":
            url = str((item.get("audio_url") or {}).get("url", ""))
            descriptors.append({"type": "audio", "hash": _sha256_bytes(url.encode("utf-8"))})
        elif item_type == "video_url":
            if video_hash_index >= len(video_hashes):
                return None
            descriptors.append({"type": "video", "hash": video_hashes[video_hash_index]})
            video_hash_index += 1
        else:
            return None
    if video_hash_index != len(video_hashes):
        return None
    return descriptors


def build_result_cache_key(
    model,
    prompt,
    seed,
    enable_random_seed,
    params,
    content,
    video_hashes=None,
    kind="video",
):
    """
    构建缓存 key；无法构建稳定 key 时返回 None。

    - enable_random_seed=True 时附加本次运行的随机 nonce：服务端随机
      seed 在提交前不可知，nonce 保证随机模式每次都 miss、正常重新
      生成，不会误复用旧结果。
    - 固定 seed 时使用实际 seed 参与 key。
    """
    descriptor = _content_descriptor(content, video_hashes)
    if descriptor is None:
        return None
    payload = {
        "v": RESULT_CACHE_ENTRY_VERSION,
        "kind": kind,
        "model": str(model),
        "prompt": normalize_prompt(prompt),
        "params": params if params is not None else {},
        "content": descriptor,
    }
    if enable_random_seed:
        payload["random_seed"] = True
        payload["nonce"] = uuid.uuid4().hex
    else:
        payload["seed"] = seed
    return _sha256_bytes(_canonical_json(payload).encode("utf-8"))


def image_reference_hashes(image_param):
    """对参考图 data URI（b64 字符串或列表）计算稳定哈希列表。"""
    if image_param is None:
        return []
    if isinstance(image_param, list):
        hashes = []
        for item in image_param:
            if not isinstance(item, str) or not item:
                return None
            hashes.append(_sha256_bytes(item.encode("utf-8")))
        return hashes
    if isinstance(image_param, str):
        return [_sha256_bytes(image_param.encode("utf-8"))]
    return None


def build_image_result_cache_key(
    model, prompt, seed, random_seed, params, image_param=None
):
    """图像生成节点缓存 key；无法稳定化时返回 None。"""
    payload = {
        "v": RESULT_CACHE_ENTRY_VERSION,
        "kind": "image",
        "model": str(model),
        "prompt": normalize_prompt(prompt),
        "params": params if params is not None else {},
    }
    image_hashes = image_reference_hashes(image_param)
    if image_hashes is None:
        return None
    if image_hashes:
        payload["images"] = image_hashes
    if random_seed:
        payload["random_seed"] = True
        payload["nonce"] = uuid.uuid4().hex
    else:
        payload["seed"] = seed
    return _sha256_bytes(_canonical_json(payload).encode("utf-8"))


class ResultCacheStore:
    """
    落盘结果缓存：index.json（元数据索引）+ files/（结果文件）。

    - 索引读写均持锁，写入采用临时文件 + os.replace 原子替换。
    - lookup 校验 TTL 与结果文件存在性，失效/缺失条目被安全移除并
      返回 None（调用方回退到正常生成）。
    - commit 先复制全部结果文件，成功后才写入索引，避免半成品命中。
    - prune 覆盖过期（TTL）与超量（上限）清理，清理失败只记日志。
    """

    def __init__(
        self,
        cache_dir,
        ttl_seconds=RESULT_CACHE_TTL_SECONDS,
        max_entries=RESULT_CACHE_MAX_ENTRIES,
    ):
        self.cache_dir = cache_dir
        self.ttl_seconds = int(ttl_seconds)
        self.max_entries = max(int(max_entries), 0)
        self.files_dir = os.path.join(cache_dir, RESULT_CACHE_FILES_DIR_NAME)
        self.index_file = os.path.join(cache_dir, RESULT_CACHE_INDEX_NAME)
        self._lock = threading.RLock()
        self._index = {}
        self.load()

    # ------------------------------------------------------------------ IO

    def resolve_path(self, rel):
        if not rel:
            return None
        try:
            path = os.path.join(self.cache_dir, str(rel))
        except Exception:
            return None
        return path if os.path.isfile(path) else None

    def load(self):
        loaded = {}
        raw_count = 0
        try:
            if os.path.exists(self.index_file):
                with open(self.index_file, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    raw_count = len(raw)
                    now_ts = time.time()
                    for key, entry in raw.items():
                        if not isinstance(key, str) or not isinstance(entry, dict):
                            continue
                        if not self._entry_structure_ok(key, entry):
                            continue
                        if float(entry.get("expire_at", 0.0) or 0.0) <= now_ts:
                            continue
                        loaded[key] = entry
        except Exception as e:
            logger.error(f"[JimengAI] Failed to load result cache index: {e}")
            loaded = {}
        with self._lock:
            self._index = loaded
            if raw_count == len(loaded):
                return
            try:
                self._prune_locked()
                self._save_locked()
            except Exception as e:
                logger.error(f"[JimengAI] Failed to prune result cache on load: {e}")

    def _save_locked(self):
        os.makedirs(self.cache_dir, exist_ok=True)
        tmp_path = f"{self.index_file}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self._index, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.index_file)

    # ---------------------------------------------------------- validation

    @staticmethod
    def _entry_structure_ok(key, entry):
        if entry.get("key") != key:
            return False
        if not isinstance(entry.get("saved_at"), (int, float)):
            return False
        if not isinstance(entry.get("expire_at"), (int, float)):
            return False
        if not isinstance(entry.get("response"), str):
            return False
        kind = entry.get("kind")
        if kind == "video":
            results = entry.get("results")
            if not isinstance(results, list) or not results:
                return False
            return all(
                isinstance(res, dict) and isinstance(res.get("video"), str)
                for res in results
            )
        if kind == "image":
            images = entry.get("images")
            counts = entry.get("frame_counts")
            if not isinstance(images, list) or not images:
                return False
            if not isinstance(counts, list) or len(counts) != len(images):
                return False
            return all(isinstance(rel, str) for rel in images)
        return False

    def _entry_files_exist(self, entry):
        kind = entry.get("kind")
        if kind == "video":
            for res in entry.get("results", []):
                if not self.resolve_path(res.get("video")):
                    return False
            return True
        if kind == "image":
            for rel in entry.get("images", []):
                if not self.resolve_path(rel):
                    return False
            return True
        return False

    # -------------------------------------------------------------- lookup

    def lookup(self, key):
        """命中返回条目；未命中、过期、文件缺失或损坏返回 None。"""
        if not key:
            return None
        with self._lock:
            entry = self._index.get(key)
            if entry is None:
                return None
            expired = float(entry.get("expire_at", 0.0) or 0.0) <= time.time()
            if expired or not self._entry_files_exist(entry):
                self._remove_entry_files_locked(entry)
                self._index.pop(key, None)
                try:
                    self._save_locked()
                except Exception as e:
                    logger.error(f"[JimengAI] Failed to persist result cache: {e}")
                return None
            return entry

    def keys(self):
        with self._lock:
            return list(self._index.keys())

    # -------------------------------------------------------------- commit

    def _copy_into_files_locked(self, src_path, copied_files):
        if not src_path or not os.path.isfile(src_path):
            return None
        ext = os.path.splitext(src_path)[1] or ".bin"
        name = f"{uuid.uuid4().hex}{ext}"
        dest_path = os.path.join(self.files_dir, name)
        os.makedirs(self.files_dir, exist_ok=True)
        shutil.copy2(src_path, dest_path)
        if os.path.getsize(dest_path) <= 0:
            return None
        rel = f"{RESULT_CACHE_FILES_DIR_NAME}/{name}"
        copied_files.append(rel)
        return rel

    def _commit_entry_locked(self, key, entry):
        entry = dict(entry)
        now_ts = time.time()
        entry.update(
            {"key": key, "saved_at": now_ts, "expire_at": now_ts + self.ttl_seconds}
        )
        self._index[key] = entry
        self._prune_locked()
        self._save_locked()
        return True

    def _commit_failed_cleanup(self, copied_files):
        for rel in copied_files:
            try:
                path = os.path.join(self.cache_dir, rel)
                if os.path.isfile(path):
                    os.remove(path)
            except Exception:
                pass

    def commit_video_result(
        self, key, model, generation_count, results, response_json
    ):
        """
        提交视频生成结果。

        results: [{"video_path", "frame_path", "task_id", "seed"}, ...]
        （video_path 为已下载的本地临时文件；全部复制成功才写索引）。
        """
        with self._lock:
            if not key or not isinstance(response_json, str) or not results:
                return False
            copied_files = []
            entry_results = []
            ok = False
            try:
                for res in results:
                    rel_video = self._copy_into_files_locked(
                        res.get("video_path"), copied_files
                    )
                    if rel_video is None:
                        return False
                    rel_frame = None
                    frame_path = res.get("frame_path")
                    if frame_path and os.path.isfile(frame_path):
                        rel_frame = self._copy_into_files_locked(frame_path, copied_files)
                        if rel_frame is None:
                            return False
                    entry_results.append(
                        {
                            "video": rel_video,
                            "frame": rel_frame,
                            "task_id": res.get("task_id"),
                            "seed": res.get("seed"),
                        }
                    )
                entry = {
                    "kind": "video",
                    "model": str(model),
                    "generation_count": generation_count,
                    "results": entry_results,
                    "response": response_json,
                }
                ok = self._commit_entry_locked(key, entry)
                return ok
            except Exception as e:
                logger.error(f"[JimengAI] Failed to commit video result cache: {e}")
                return False
            finally:
                if not ok:
                    self._commit_failed_cleanup(copied_files)

    def commit_image_result(
        self, key, model, image_blobs, frame_counts, response_json
    ):
        """
        提交图像生成结果。

        image_blobs: list[bytes]（已编码的 PNG 字节）；
        frame_counts: 与每个请求对应的结果帧数列表。
        """
        with self._lock:
            if not key or not isinstance(response_json, str):
                return False
            if not image_blobs or not isinstance(frame_counts, list):
                return False
            if len(frame_counts) == 0 or not all(
                isinstance(count, int) and count > 0 for count in frame_counts
            ):
                return False
            copied_files = []
            rel_paths = []
            ok = False
            try:
                os.makedirs(self.files_dir, exist_ok=True)
                for blob in image_blobs:
                    if not blob:
                        return False
                    name = f"{uuid.uuid4().hex}.png"
                    dest_path = os.path.join(self.files_dir, name)
                    with open(dest_path, "wb") as f:
                        f.write(blob)
                    if os.path.getsize(dest_path) <= 0:
                        return False
                    rel = f"{RESULT_CACHE_FILES_DIR_NAME}/{name}"
                    copied_files.append(rel)
                    rel_paths.append(rel)
                total_frames = sum(frame_counts)
                if total_frames != len(rel_paths):
                    return False
                entry = {
                    "kind": "image",
                    "model": str(model),
                    "images": rel_paths,
                    "frame_counts": frame_counts,
                    "response": response_json,
                }
                ok = self._commit_entry_locked(key, entry)
                return ok
            except Exception as e:
                logger.error(f"[JimengAI] Failed to commit image result cache: {e}")
                return False
            finally:
                if not ok:
                    self._commit_failed_cleanup(copied_files)

    # --------------------------------------------------------------- prune

    def _entry_rel_files(self, entry):
        rels = []
        if not isinstance(entry, dict):
            return rels
        if entry.get("kind") == "video":
            for res in entry.get("results", []):
                if isinstance(res, dict):
                    for rel in (res.get("video"), res.get("frame")):
                        if isinstance(rel, str):
                            rels.append(rel)
        elif entry.get("kind") == "image":
            for rel in entry.get("images", []):
                if isinstance(rel, str):
                    rels.append(rel)
        return rels

    def _remove_entry_files_locked(self, entry):
        for rel in self._entry_rel_files(entry):
            try:
                path = os.path.join(self.cache_dir, rel)
                if os.path.isfile(path):
                    os.remove(path)
            except Exception:
                pass

    def _prune_locked(self):
        now_ts = time.time()
        for key in list(self._index.keys()):
            entry = self._index.get(key)
            if not isinstance(entry, dict) or float(
                entry.get("expire_at", 0.0) or 0.0
            ) <= now_ts:
                self._remove_entry_files_locked(entry)
                self._index.pop(key, None)
        if len(self._index) > self.max_entries:
            sorted_keys = sorted(
                self._index.keys(),
                key=lambda k: float(self._index[k].get("saved_at", 0.0) or 0.0),
            )
            remove_count = len(self._index) - self.max_entries
            for key in sorted_keys[:remove_count]:
                self._remove_entry_files_locked(self._index.get(key))
                self._index.pop(key, None)
        self._sweep_orphan_files_locked()

    def _sweep_orphan_files_locked(self):
        try:
            if not os.path.isdir(self.files_dir):
                return
            referenced = set()
            for entry in self._index.values():
                referenced.update(self._entry_rel_files(entry))
            for name in os.listdir(self.files_dir):
                rel = f"{RESULT_CACHE_FILES_DIR_NAME}/{name}"
                if rel not in referenced:
                    path = os.path.join(self.files_dir, name)
                    if os.path.isfile(path):
                        os.remove(path)
        except Exception as e:
            logger.error(f"[JimengAI] Failed to sweep orphan result cache files: {e}")
