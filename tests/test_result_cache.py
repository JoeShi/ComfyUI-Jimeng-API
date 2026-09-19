"""
生成结果持久化缓存测试（WEI-11）。

覆盖：
- 缓存 key 的稳定性与敏感性（参数 / 素材内容 / seed / 随机种子 nonce）。
- 落盘 store 的 TTL、容量、原子写、损坏 / 缺失安全降级与并发。
- 视频节点 `_common_generation_logic`：相同输入二次命中且不新增提交、
  跨 store 实例（模拟进程重载）仍命中、输出文件与 response 等价。
- 图像节点 `JimengSeedream4.execute`：相同输入二次命中且不新增请求、
  输出张量数值等价。
全程 mock（无真实凭证、无网络、无真实提交）。
"""

import asyncio
import importlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
from types import SimpleNamespace


PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ensure_real_numpy_pil():
    """
    已有测试文件会用空壳桩占用 numpy / PIL（sys.modules.setdefault）。
    本测试需要真实 numpy + PIL 做张量-PNG 往返校验，遇到桩时移除并
    重新导入；其它测试文件已绑定的引用不受影响。
    """
    numpy_mod = sys.modules.get("numpy")
    if numpy_mod is not None and not hasattr(numpy_mod, "asarray"):
        del sys.modules["numpy"]
    pil_image = sys.modules.get("PIL.Image")
    if pil_image is not None and not hasattr(pil_image, "open"):
        for name in ("PIL.Image", "PIL"):
            sys.modules.pop(name, None)
    import numpy  # noqa: F401
    import PIL.Image  # noqa: F401


_ensure_real_numpy_pil()

import numpy  # noqa: E402
import PIL.Image  # noqa: E402


def _install_stub(name, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules.setdefault(name, module)
    return module


class FakeTensor:
    """numpy 数组包装的 torch.Tensor 桩，覆盖测试路径所需接口。"""

    def __init__(self, arr):
        self.arr = arr

    @property
    def shape(self):
        return self.arr.shape

    @property
    def ndim(self):
        return self.arr.ndim

    def __getitem__(self, item):
        return FakeTensor(self.arr[item])

    def cpu(self):
        return self

    def numpy(self):
        return self.arr

    def detach(self):
        return self

    def __repr__(self):
        return f"FakeTensor(shape={self.arr.shape})"


def _install_heavy_dependency_stubs():
    _install_stub("folder_paths")
    _install_stub("cv2")
    _install_stub("requests")

    # torch 桩（若已被其它测试占用则在其上补齐所需属性）
    torch_module = sys.modules.get("torch") or _install_stub("torch")
    torch_module.from_numpy = lambda a: FakeTensor(a)
    torch_module.Tensor = FakeTensor
    torch_module.cat = lambda ts, dim=0: (
        ts[0] if len(ts) == 1 else FakeTensor(numpy.concatenate([t.arr for t in ts], axis=0))
    )
    torch_module.ones = lambda *args, **kwargs: FakeTensor(numpy.ones(args))
    torch_module.clamp = lambda t, lo, hi: t
    torch_module.zeros = lambda *args, **kwargs: FakeTensor(numpy.zeros(args))
    nn_module = sys.modules.get("torch.nn") or _install_stub("torch.nn")
    functional_module = sys.modules.get("torch.nn.functional") or _install_stub(
        "torch.nn.functional"
    )
    functional_module.interpolate = lambda *args, **kwargs: args[0]
    nn_module.functional = functional_module
    torch_module.nn = nn_module

    # aiohttp 桩
    class _FakeClientSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    aiohttp_module = sys.modules.get("aiohttp") or _install_stub("aiohttp")
    aiohttp_module.ClientSession = _FakeClientSession
    aiohttp_module.ClientTimeout = lambda **kwargs: None
    aiohttp_module.TCPConnector = lambda **kwargs: None
    aiohttp_module.ClientError = type("ClientError", (Exception,), {})

    # server / comfy 桩
    class _InterruptProcessingException(Exception):
        pass

    def _noop_interrupt():
        return None

    server_module = _install_stub("server")
    server_module.PromptServer = SimpleNamespace(
        instance=SimpleNamespace(
            prompt=None,
            send_sync=lambda *args, **kwargs: None,
            send_progress_text=lambda *args, **kwargs: None,
        )
    )

    comfy_module = _install_stub("comfy")
    mm_module = _install_stub("comfy.model_management")
    mm_module.throw_exception_if_processing_interrupted = _noop_interrupt
    mm_module.InterruptProcessingException = _InterruptProcessingException
    comfy_module.model_management = mm_module

    # volcenginesdkarkruntime 桩（含 executor / nodes_image 使用的子模块）
    class _FakeArk:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    ark_sdk = _install_stub("volcenginesdkarkruntime", Ark=_FakeArk)
    _install_stub("volcenginesdkarkruntime.types")
    _install_stub("volcenginesdkarkruntime.types.responses")
    for leaf, symbol in (
        ("response_completed_event", "ResponseCompletedEvent"),
        ("response_reasoning_summary_text_delta_event", "ResponseReasoningSummaryTextDeltaEvent"),
        ("response_output_item_added_event", "ResponseOutputItemAddedEvent"),
        ("response_text_delta_event", "ResponseTextDeltaEvent"),
        ("response_text_done_event", "ResponseTextDoneEvent"),
        ("response_reasoning_text_delta_event", "ResponseReasoningTextDeltaEvent"),
    ):
        _install_stub(
            f"volcenginesdkarkruntime.types.responses.{leaf}",
            **{symbol: SimpleNamespace()},
        )
    _install_stub("volcenginesdkarkruntime.types.images")
    _install_stub(
        "volcenginesdkarkruntime.types.images.images",
        SequentialImageGenerationOptions=SimpleNamespace,
        ContentGenerationTool=SimpleNamespace,
        OptimizePromptOptions=SimpleNamespace,
    )

    # folder_paths 具体行为
    folder_paths_module = sys.modules["folder_paths"]
    temp_dir = tempfile.mkdtemp(prefix="jimeng_rc_temp_")
    output_dir = tempfile.mkdtemp(prefix="jimeng_rc_out_")

    def _get_temp_directory():
        return temp_dir

    def _get_output_directory():
        return output_dir

    _save_path_counters = {}

    def _get_save_image_path(prefix, output_dirname):
        subfolder = os.path.dirname(prefix or "")
        folder = os.path.join(output_dirname, subfolder) if subfolder else output_dirname
        os.makedirs(folder, exist_ok=True)
        base = os.path.basename(prefix or "") or "file"
        _save_path_counters.setdefault((folder, base), 0)
        _save_path_counters[(folder, base)] += 1
        counter = _save_path_counters[(folder, base)]
        return folder, base, counter, subfolder, base

    folder_paths_module.get_temp_directory = _get_temp_directory
    folder_paths_module.get_output_directory = _get_output_directory
    folder_paths_module.get_save_image_path = _get_save_image_path

    # comfy_api 桩
    comfy_api = sys.modules.get("comfy_api") or _install_stub("comfy_api")
    comfy_api.latest = sys.modules.get("comfy_api.latest") or _install_stub(
        "comfy_api.latest"
    )

    class _InputSpec:
        def __init__(self, input_id, **kwargs):
            self.id = input_id
            self.__dict__.update(kwargs)

    class _WidgetSpec:
        @staticmethod
        def Input(input_id, options=None, default=None, **kwargs):
            return _InputSpec(input_id, options=options, default=default, **kwargs)

        @staticmethod
        def Output(input_id=None, **kwargs):
            return _InputSpec(input_id, **kwargs)

    class _CustomType:
        def __init__(self, type_name):
            self.type_name = type_name

        def Input(self, input_id=None, **kwargs):
            return _InputSpec(input_id, **kwargs)

        Output = Input

    class _ComfyNode:
        hidden = SimpleNamespace(unique_id="1", prompt=None)

    class _VideoFromFile:
        def __init__(self, path):
            self.path = path

    def _node_output(*args, **kwargs):
        # 同时暴露 args 与 value（兼容其它测试模块对 NodeOutput.value 的访问）
        return SimpleNamespace(args=args, value=args[0] if args else None, **kwargs)

    comfy_api.latest.io = SimpleNamespace(
        ComfyNode=_ComfyNode,
        Schema=lambda **kwargs: SimpleNamespace(**kwargs),
        String=_WidgetSpec,
        Combo=_WidgetSpec,
        Int=_WidgetSpec,
        Float=_WidgetSpec,
        Boolean=_WidgetSpec,
        Image=_WidgetSpec,
        Video=_WidgetSpec,
        Audio=_WidgetSpec,
        Custom=lambda type_name: _CustomType(type_name),
        NodeOutput=_node_output,
        Hidden=SimpleNamespace(
            unique_id="unique_id",
            prompt="prompt",
            auth_token_comfy_org="auth_token_comfy_org",
            api_key_comfy_org="api_key_comfy_org",
        ),
        NumberDisplay=SimpleNamespace(number="number"),
        Autogrow=SimpleNamespace(
            Input=lambda *args, **kwargs: SimpleNamespace(),
            TemplatePrefix=lambda **kwargs: SimpleNamespace(),
            TemplateNames=lambda **kwargs: SimpleNamespace(),
        ),
        DynamicCombo=SimpleNamespace(
            Input=lambda *args, **kwargs: SimpleNamespace(),
            Option=lambda *args, **kwargs: SimpleNamespace(),
        ),
    )

    comfy_api.input_impl = sys.modules.get("comfy_api.input_impl") or _install_stub(
        "comfy_api.input_impl"
    )
    comfy_api.input_impl.VideoFromFile = _VideoFromFile


def _load_plugin_modules():
    package_name = "jimeng_result_cache_test"
    if package_name not in sys.modules:
        package = types.ModuleType(package_name)
        package.__path__ = [PLUGIN_ROOT]
        sys.modules[package_name] = package

    return (
        importlib.import_module(f"{package_name}.nodes.result_cache"),
        importlib.import_module(f"{package_name}.nodes.nodes_video"),
        importlib.import_module(f"{package_name}.nodes.nodes_image"),
    )


_install_heavy_dependency_stubs()
result_cache, nodes_video, nodes_image = _load_plugin_modules()


class FakeVideo:
    """comfy 视频对象桩：get_stream_source 返回文件路径。"""

    def __init__(self, path):
        self._path = path

    def get_stream_source(self):
        return self._path


class ResultCacheKeyTests(unittest.TestCase):
    def _video_key(self, **overrides):
        params = {
            "service_tier": "default",
            "return_last_frame": True,
            "api_params": {
                "resolution": "720p",
                "ratio": "16:9",
                "seed": 42,
                "duration": 5,
                "generate_audio": True,
            },
        }
        prompt = overrides.pop("prompt", "a cat")
        seed = overrides.pop("seed", 42)
        enable_random = overrides.pop("enable_random_seed", False)
        content = overrides.pop("content", [])
        video_hashes = overrides.pop("video_hashes", None)
        params.update(overrides.pop("params_extra", {}))
        return result_cache.build_result_cache_key(
            "doubao-seedance-2-5-260628",
            prompt,
            seed,
            enable_random,
            params,
            content,
            video_hashes,
            kind="video",
        )

    def test_same_inputs_produce_same_key(self):
        self.assertEqual(self._video_key(), self._video_key())

    def test_prompt_is_normalized_by_stripping(self):
        self.assertEqual(self._video_key(prompt="  a cat  \n"), self._video_key())

    def test_different_prompt_seed_params_produce_different_keys(self):
        base = self._video_key()
        self.assertNotEqual(base, self._video_key(prompt="a dog"))
        self.assertNotEqual(base, self._video_key(seed=43))
        self.assertNotEqual(
            base,
            self._video_key(
                params_extra={"api_params": {
                    "resolution": "720p", "ratio": "16:9", "seed": 42,
                    "duration": 5, "generate_audio": False,
                }}
            ),
        )
        self.assertNotEqual(
            base,
            self._video_key(
                params_extra={"api_params": {
                    "resolution": "1080p", "ratio": "16:9", "seed": 42,
                    "duration": 5, "generate_audio": True,
                }}
            ),
        )
        self.assertNotEqual(
            base,
            self._video_key(
                params_extra={"api_params": {
                    "resolution": "720p", "ratio": "16:9", "seed": 42,
                    "duration": 5, "generate_audio": True, "tools": [{"type": "web_search"}],
                }}
            ),
        )

    def test_random_seed_mode_uses_per_run_nonce(self):
        key1 = self._video_key(enable_random_seed=True)
        key2 = self._video_key(enable_random_seed=True)
        self.assertNotEqual(key1, key2)

    def test_different_model_produces_different_key(self):
        params = {"seed": 1}
        k1 = result_cache.build_result_cache_key("m-a", "p", 1, False, params, [])
        k2 = result_cache.build_result_cache_key("m-b", "p", 1, False, params, [])
        self.assertNotEqual(k1, k2)

    def test_reference_image_content_changes_key(self):
        content1 = [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAA"}, "role": "first_frame"}
        ]
        content2 = [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,BBB"}, "role": "first_frame"}
        ]
        self.assertNotEqual(
            self._video_key(content=content1), self._video_key(content=content2)
        )

    def test_reference_audio_content_changes_key(self):
        content1 = [{"type": "audio_url", "audio_url": {"url": "data:audio/wav;base64,AAA"}, "role": "reference_audio"}]
        content2 = [{"type": "audio_url", "audio_url": {"url": "data:audio/wav;base64,BBB"}, "role": "reference_audio"}]
        self.assertNotEqual(
            self._video_key(content=content1), self._video_key(content=content2)
        )

    def test_reference_video_requires_content_hash(self):
        content = [{"type": "video_url", "video_url": {"url": "https://example/v.mp4"}, "role": "reference_video"}]
        # 无内容哈希 → 无法构建稳定 key
        self.assertIsNone(self._video_key(content=content, video_hashes=None))
        # 哈希数量不匹配 → 同样拒绝
        self.assertIsNone(self._video_key(content=content, video_hashes=["h1", "h2"]))
        # 提供哈希 → 可构建，且不同哈希产生不同 key
        k1 = self._video_key(content=content, video_hashes=["h1"])
        k2 = self._video_key(content=content, video_hashes=["h2"])
        self.assertIsNotNone(k1)
        self.assertNotEqual(k1, k2)

    def test_unsupported_content_types_rejected(self):
        self.assertIsNone(
            self._video_key(content=[{"type": "draft_task", "draft_task": {"id": "x"}}])
        )
        # 多 content（list of list）不支持
        self.assertIsNone(self._video_key(content=[[{"type": "text", "text": "a"}]]))

    def test_stable_video_hash_is_content_based_path_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path_a = os.path.join(tmp, "a.mp4")
            path_b = os.path.join(tmp, "b.mp4")
            path_c = os.path.join(tmp, "c.mp4")
            with open(path_a, "wb") as f:
                f.write(b"video-bytes-123")
            with open(path_b, "wb") as f:
                f.write(b"video-bytes-123")
            with open(path_c, "wb") as f:
                f.write(b"other-content")
            self.assertEqual(
                result_cache.stable_video_hash(FakeVideo(path_a)),
                result_cache.stable_video_hash(FakeVideo(path_b)),
            )
            self.assertNotEqual(
                result_cache.stable_video_hash(FakeVideo(path_a)),
                result_cache.stable_video_hash(FakeVideo(path_c)),
            )
            self.assertIsNone(result_cache.stable_video_hash(FakeVideo(os.path.join(tmp, "missing.mp4"))))
            self.assertIsNone(result_cache.stable_video_hash(None))
            self.assertIsNone(result_cache.stable_video_hash(SimpleNamespace()))

    def test_stable_video_hash_supports_buffers(self):
        import io

        class _BufferVideo:
            def get_stream_source(self):
                return io.BytesIO(b"buffer-content")

        class _BytesVideo:
            def __init__(self):
                self._value = b"buffer-content"

            def getvalue(self):
                return self._value

            def get_stream_source(self):
                return self

        self.assertEqual(
            result_cache.stable_video_hash(_BufferVideo()),
            result_cache.stable_video_hash(_BytesVideo()),
        )

    def test_image_reference_hashes(self):
        self.assertEqual(result_cache.image_reference_hashes(None), [])
        self.assertEqual(
            result_cache.image_reference_hashes("data:image/jpeg;base64,AAA"),
            result_cache.image_reference_hashes("data:image/jpeg;base64,AAA"),
        )
        self.assertNotEqual(
            result_cache.image_reference_hashes("data:image/jpeg;base64,AAA"),
            result_cache.image_reference_hashes("data:image/jpeg;base64,BBB"),
        )
        self.assertIsNone(result_cache.image_reference_hashes(12345))

    def test_image_key_sensitivity(self):
        params = {"size": "2K", "watermark": False}
        base = result_cache.build_image_result_cache_key(
            "doubao-seedream-4-0-250828", "a cat", 7, False, params
        )
        self.assertEqual(
            base,
            result_cache.build_image_result_cache_key(
                "doubao-seedream-4-0-250828", " a cat ", 7, False, params
            ),
        )
        self.assertNotEqual(
            base,
            result_cache.build_image_result_cache_key(
                "doubao-seedream-4-0-250828", "a cat", 8, False, params
            ),
        )
        self.assertNotEqual(
            base,
            result_cache.build_image_result_cache_key(
                "doubao-seedream-4-0-250828", "a cat", 7, False, {"size": "4K", "watermark": False}
            ),
        )
        self.assertNotEqual(
            base,
            result_cache.build_image_result_cache_key(
                "doubao-seedream-4-0-250828",
                "a cat",
                7,
                False,
                params,
                image_param="data:image/jpeg;base64,AAA",
            ),
        )
        self.assertNotEqual(
            result_cache.build_image_result_cache_key(
                "m", "p", -1, True, params
            ),
            result_cache.build_image_result_cache_key("m", "p", -1, True, params),
        )


class _StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.cache_dir = tempfile.mkdtemp(prefix="jimeng_rc_store_")
        self.store = result_cache.ResultCacheStore(self.cache_dir)

    def tearDown(self):
        shutil.rmtree(self.cache_dir, ignore_errors=True)

    def _make_media_file(self, name, data=b"media-bytes"):
        path = os.path.join(self.cache_dir, f"src_{name}")
        with open(path, "wb") as f:
            f.write(data)
        return path

    def _commit_video(self, key, tag, results=None):
        if results is None:
            video_path = self._make_media_file(f"{tag}.mp4", f"video-{tag}".encode())
            frame_path = self._make_media_file(f"{tag}.jpg", f"frame-{tag}".encode())
            results = [
                {
                    "video_path": video_path,
                    "frame_path": frame_path,
                    "task_id": f"cgt-{tag}",
                    "seed": 1,
                }
            ]
        return self.store.commit_video_result(
            key, "model-x", 1, results, json.dumps([{"id": f"cgt-{tag}"}])
        )

    def _commit_image(self, key, tag, blobs=None, counts=None):
        if blobs is None:
            arr = (numpy.arange(16, dtype=numpy.float32) / 255.0).reshape(2, 2, 4)[:, :, :3]
            img = PIL.Image.fromarray((arr * 255.0).astype(numpy.uint8))
            import io as _io

            buffer = _io.BytesIO()
            img.save(buffer, format="PNG")
            blobs = [buffer.getvalue()]
        return self.store.commit_image_result(
            key, "model-img", blobs, counts or [len(blobs)], json.dumps([{"id": f"img-{tag}"}])
        )


class ResultCacheStoreTests(_StoreTestCase):
    def test_commit_and_lookup_video(self):
        self.assertTrue(self._commit_video("k1", "a"))
        entry = self.store.lookup("k1")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["kind"], "video")
        self.assertEqual(entry["results"][0]["task_id"], "cgt-a")
        video_path = self.store.resolve_path(entry["results"][0]["video"])
        frame_path = self.store.resolve_path(entry["results"][0]["frame"])
        with open(video_path, "rb") as f:
            self.assertEqual(f.read(), b"video-a")
        with open(frame_path, "rb") as f:
            self.assertEqual(f.read(), b"frame-a")

    def test_lookup_missing_key_returns_none(self):
        self.assertIsNone(self.store.lookup("nope"))

    def test_missing_result_file_degrades_and_purges(self):
        self.assertTrue(self._commit_video("k1", "a"))
        entry = self.store.lookup("k1")
        video_path = self.store.resolve_path(entry["results"][0]["video"])
        os.remove(video_path)
        self.assertIsNone(self.store.lookup("k1"))
        self.assertNotIn("k1", self.store.keys())

    def test_ttl_expiry_causes_miss_and_cleanup(self):
        self.assertTrue(self._commit_video("k1", "a"))
        with self.store._lock:
            entry = dict(self.store._index["k1"])
            entry["expire_at"] = time.time() - 1
            self.store._index["k1"] = entry
            self.store._save_locked()
        reloaded = result_cache.ResultCacheStore(self.cache_dir)
        self.assertEqual(reloaded.keys(), [])
        self.assertEqual(os.listdir(reloaded.files_dir), [])

    def test_capacity_evicts_oldest(self):
        store = result_cache.ResultCacheStore(self.cache_dir, max_entries=2)
        self.store = store
        self.assertTrue(self._commit_video("k1", "a"))
        time.sleep(0.01)
        self.assertTrue(self._commit_video("k2", "b"))
        time.sleep(0.01)
        self.assertTrue(self._commit_video("k3", "c"))
        self.assertEqual(sorted(store.keys()), ["k2", "k3"])
        # 被逐出条目的文件被删除
        remaining = set(os.listdir(store.files_dir))
        for name in remaining:
            with open(os.path.join(store.files_dir, name), "rb") as f:
                data = f.read()
            self.assertNotEqual(data, b"video-a")

    def test_commit_is_all_or_nothing_when_source_missing(self):
        good = self._make_media_file("good.mp4")
        results = [
            {"video_path": good, "frame_path": None, "task_id": "t1", "seed": 1},
            {"video_path": os.path.join(self.cache_dir, "missing.mp4"), "frame_path": None, "task_id": "t2", "seed": 2},
        ]
        self.assertFalse(
            self.store.commit_video_result("k1", "m", 2, results, "[]")
        )
        self.assertIsNone(self.store.lookup("k1"))
        # 无半成品文件残留（orphan sweep 不产生误命中）
        self.assertEqual(os.listdir(self.store.files_dir), [])

    def test_corrupt_index_loads_empty(self):
        self.assertTrue(self._commit_video("k1", "a"))
        with open(self.store.index_file, "w", encoding="utf-8") as f:
            f.write("{not-valid-json!!")
        reloaded = result_cache.ResultCacheStore(self.cache_dir)
        self.assertEqual(reloaded.keys(), [])
        self.assertIsNone(reloaded.lookup("k1"))

    def test_reload_from_disk_keeps_hits(self):
        self.assertTrue(self._commit_video("k1", "a"))
        reloaded = result_cache.ResultCacheStore(self.cache_dir)
        entry = reloaded.lookup("k1")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["results"][0]["task_id"], "cgt-a")

    def test_concurrent_commits_keep_index_consistent(self):
        # 模拟同进程多线程并发写（生产环境为单例 store，RLock 串行化写入）
        store = result_cache.ResultCacheStore(self.cache_dir)
        errors = []

        def worker(idx):
            try:
                for j in range(4):
                    video_path = self._make_media_file(f"w{idx}_{j}.mp4", f"w{idx}-{j}".encode())
                    ok = store.commit_video_result(
                        f"key-{idx}-{j}",
                        "m",
                        1,
                        [{"video_path": video_path, "frame_path": None, "task_id": "t", "seed": 1}],
                        json.dumps([{"id": "t"}]),
                    )
                    if not ok:
                        errors.append((idx, j))
            except Exception as e:  # pragma: no cover
                errors.append((idx, repr(e)))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        with open(store.index_file, encoding="utf-8") as f:
            index = json.load(f)
        self.assertEqual(len(index), 24)
        for i in range(6):
            for j in range(4):
                self.assertIsNotNone(store.lookup(f"key-{i}-{j}"))

    def test_commit_and_lookup_image(self):
        self.assertTrue(self._commit_image("ik1", "a"))
        entry = self.store.lookup("ik1")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["kind"], "image")
        self.assertEqual(len(entry["images"]), 1)
        path = self.store.resolve_path(entry["images"][0])
        image = PIL.Image.open(path)
        self.assertEqual(image.size, (2, 2))

    def test_image_frame_counts_mismatch_rejected(self):
        self.assertFalse(self._commit_image("ik1", "a", counts=[2]))
        self.assertIsNone(self.store.lookup("ik1"))

    def test_orphan_files_are_swept(self):
        self.assertTrue(self._commit_video("k1", "a"))
        orphan = os.path.join(self.store.files_dir, "orphan.mp4")
        with open(orphan, "wb") as f:
            f.write(b"junk")
        # 触发一次 prune（通过再次 commit）
        self.assertTrue(self._commit_video("k2", "b"))
        self.assertFalse(os.path.exists(orphan))


class VideoResultCacheIntegrationTests(unittest.TestCase):
    """通过 JimengVideoBase._common_generation_logic 验证提交跳过与输出等价。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jimeng_rc_video_")
        self.cache_dir = os.path.join(self.tmp, "result_cache")
        self.store = result_cache.ResultCacheStore(self.cache_dir)

        self._old_get_store = result_cache.get_result_cache_store
        result_cache.get_result_cache_store = lambda: self.store

        self._old_run_batch = nodes_video.JimengGenerationExecutor.run_batch_tasks
        self.submit_calls = []
        self._last_fake_tasks = None
        nodes_video.JimengGenerationExecutor.run_batch_tasks = self._fake_run_batch_tasks

        self._old_dl_video = nodes_video.download_video_to_temp
        self._old_dl_image = nodes_video.download_image_to_temp
        self.download_counts = {"video": 0, "frame": 0}
        nodes_video.download_video_to_temp = self._fake_download_video
        nodes_video.download_image_to_temp = self._fake_download_image

        self._old_output_dir = sys.modules["folder_paths"].get_output_directory
        self.output_dir = os.path.join(self.tmp, "out")
        os.makedirs(self.output_dir, exist_ok=True)
        sys.modules["folder_paths"].get_output_directory = lambda: self.output_dir

        self.fake_client = SimpleNamespace(
            ark=SimpleNamespace(),
            api_key=None,
            check_quota=lambda *args, **kwargs: None,
            update_usage=lambda *args, **kwargs: None,
        )

    def tearDown(self):
        result_cache.get_result_cache_store = self._old_get_store
        nodes_video.JimengGenerationExecutor.run_batch_tasks = self._old_run_batch
        nodes_video.download_video_to_temp = self._old_dl_video
        nodes_video.download_image_to_temp = self._old_dl_image
        sys.modules["folder_paths"].get_output_directory = self._old_output_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ fakes

    def _fake_run_batch_tasks(self, **kwargs):
        async def _run(**kw):
            self.submit_calls.append(kw)
            count = int(kw.get("generation_count", 1))
            tasks = []
            for i in range(count):
                task = SimpleNamespace(
                    id=f"cgt-{len(self.submit_calls)}-{i}",
                    status="succeeded",
                    seed=42 + i,
                    content=SimpleNamespace(
                        video_url="https://example.com/video.mp4",
                        last_frame_url="https://example.com/frame.jpg",
                    ),
                    model_dump=lambda tid=f"cgt-{len(self.submit_calls)}-{i}": {
                        "id": tid,
                        "status": "succeeded",
                        "usage": {"completion_tokens": 1234},
                    },
                )
                tasks.append(task)
            return tasks

        return _run(**kwargs)

    async def _fake_download_video(self, session, url, prefix, seed, save_path):
        self.download_counts["video"] += 1
        path = os.path.join(self.tmp, f"dl_video_{self.download_counts['video']}.mp4")
        with open(path, "wb") as f:
            f.write(b"downloaded-video-bytes")
        return path

    async def _fake_download_image(self, session, url, prefix, seed, save_path):
        self.download_counts["frame"] += 1
        path = os.path.join(self.tmp, f"dl_frame_{self.download_counts['frame']}.jpg")
        arr = (numpy.ones((4, 6, 3)) * 0.5).astype(numpy.uint8)
        PIL.Image.fromarray(arr).save(path, format="JPEG")
        tensor = nodes_video.torch.from_numpy(arr.astype(numpy.float32) / 255.0)[None]
        return (tensor, path)

    # ------------------------------------------------------------ helpers

    def _run_node(self, **overrides):
        defaults = dict(
            client=self.fake_client,
            prompt="a cat",
            duration=5,
            resolution="720p",
            aspect_ratio="16:9",
            seed=42,
            generation_count=1,
            filename_prefix="Jimeng/Test",
            save_last_frame_batch=False,
            non_blocking=False,
            node_id="1",
            model_name="doubao-seedance-2-5-260628",
            content=[],
            forbidden_params=[],
            enable_random_seed=False,
            use_result_cache=True,
        )
        defaults.update(overrides)

        async def _go():
            helper = nodes_video.JimengVideoBase()
            return await helper._common_generation_logic(**defaults)

        return asyncio.run(_go())

    # ------------------------------------------------------------ tests

    def test_same_input_hits_cache_without_resubmission(self):
        out1 = self._run_node()
        self.assertEqual(len(self.submit_calls), 1)
        task_id_1 = json.loads(out1.args[2])[0]["id"]

        out2 = self._run_node()
        self.assertEqual(len(self.submit_calls), 1, "second run must not submit again")
        task_id_2 = json.loads(out2.args[2])[0]["id"]
        self.assertEqual(task_id_1, task_id_2, "cached response keeps original task id")

        video1, video2 = out1.args[0], out2.args[0]
        self.assertTrue(os.path.isfile(video1.path))
        self.assertTrue(os.path.isfile(video2.path))
        self.assertNotEqual(video1.path, video2.path, "hit path rebuilds from cache dir")
        with open(video1.path, "rb") as f:
            b1 = f.read()
        with open(video2.path, "rb") as f:
            b2 = f.read()
        self.assertEqual(b1, b2)
        self.assertEqual(out1.args[2], out2.args[2])
        # 缓存文件确实落在插件约定目录
        cache_video = self.store.resolve_path(
            self.store.lookup(
                result_cache.build_result_cache_key(
                    "doubao-seedance-2-5-260628",
                    "a cat",
                    42,
                    False,
                    {
                        "service_tier": "default",
                        "return_last_frame": True,
                        "api_params": {
                            "resolution": "720p",
                            "ratio": "16:9",
                            "seed": 42,
                            "duration": 5,
                        },
                    },
                    [],
                    None,
                    kind="video",
                )
            )["results"][0]["video"]
        )
        self.assertTrue(cache_video.startswith(self.cache_dir))

    def test_hit_rebuilds_equivalent_last_frame(self):
        out1 = self._run_node()
        frame1 = out1.args[1]
        out2 = self._run_node()
        frame2 = out2.args[1]
        self.assertIsNotNone(frame1)
        self.assertIsNotNone(frame2)
        numpy.testing.assert_allclose(
            frame1.numpy(), frame2.numpy(), atol=1.0 / 255.0
        )

    def test_different_inputs_miss(self):
        self._run_node()
        self.assertEqual(len(self.submit_calls), 1)

        self._run_node(seed=43)
        self.assertEqual(len(self.submit_calls), 2)

        self._run_node(prompt="a dog")
        self.assertEqual(len(self.submit_calls), 3)

        self._run_node(duration=6)
        self.assertEqual(len(self.submit_calls), 4)

        self._run_node(resolution="480p")
        self.assertEqual(len(self.submit_calls), 5)

    def test_different_reference_image_content_misses(self):
        content_a = [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAA"}, "role": "first_frame"}
        ]
        content_b = [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,BBB"}, "role": "first_frame"}
        ]
        # content 会被生成逻辑插入 prompt，故每次传入全新列表（与真实节点每次重建 content 一致）
        self._run_node(content=list(content_a))
        self._run_node(content=list(content_a))
        self.assertEqual(len(self.submit_calls), 1)
        self._run_node(content=list(content_b))
        self.assertEqual(len(self.submit_calls), 2)

    def test_random_seed_mode_always_misses(self):
        self._run_node(enable_random_seed=True)
        self._run_node(enable_random_seed=True)
        self.assertEqual(len(self.submit_calls), 2)

    def test_node_id_not_part_of_cache_key(self):
        self._run_node(node_id="1")
        self._run_node(node_id="99")
        self.assertEqual(len(self.submit_calls), 1)

    def test_toggle_off_always_submits(self):
        self._run_node(use_result_cache=False)
        self._run_node(use_result_cache=False)
        self.assertEqual(len(self.submit_calls), 2)
        self.assertEqual(self.store.keys(), [])

    def test_cache_survives_store_reload(self):
        self._run_node()
        reloaded_store = result_cache.ResultCacheStore(self.cache_dir)
        self.store = reloaded_store
        self._run_node()
        self.assertEqual(len(self.submit_calls), 1)

    def test_missing_cache_file_falls_back_to_generation(self):
        self._run_node()
        entry = self.store.lookup(
            list(self.store.keys())[0]
        )
        os.remove(self.store.resolve_path(entry["results"][0]["video"]))
        self._run_node()
        self.assertEqual(len(self.submit_calls), 2)

    def test_draft_mode_not_cached(self):
        self._run_node(extra_api_params={"draft": True})
        self._run_node(extra_api_params={"draft": True})
        self.assertEqual(len(self.submit_calls), 2)
        self.assertEqual(self.store.keys(), [])

    def test_batch_hit_saves_outputs_equivalently(self):
        before = self._count_output_files()
        out1 = self._run_node(generation_count=2)
        mid = self._count_output_files()
        self.assertEqual(mid - before, 2)
        out2 = self._run_node(generation_count=2)
        after = self._count_output_files()
        self.assertEqual(after - mid, 2, "hit must replicate batch save-to-output")
        self.assertEqual(len(self.submit_calls), 1)
        self.assertEqual(
            json.loads(out1.args[2])[0]["id"], json.loads(out2.args[2])[0]["id"]
        )

    def _count_output_files(self):
        count = 0
        for _root, _dirs, files in os.walk(self.output_dir):
            count += len(files)
        return count


class ImageResultCacheIntegrationTests(unittest.TestCase):
    """通过 JimengSeedream4.execute 验证图像节点缓存行为。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jimeng_rc_img_")
        self.cache_dir = os.path.join(self.tmp, "result_cache")
        self.store = result_cache.ResultCacheStore(self.cache_dir)

        self._old_get_store = result_cache.get_result_cache_store
        result_cache.get_result_cache_store = lambda: self.store

        self._old_run_parallel = nodes_image.JimengGenerationExecutor.run_parallel_requests
        self.request_count = []
        nodes_image.JimengGenerationExecutor.run_parallel_requests = (
            self._fake_run_parallel_requests
        )

        self.fake_client = SimpleNamespace(
            ark=SimpleNamespace(),
            api_key=None,
            check_quota=lambda *args, **kwargs: None,
            update_usage=lambda *args, **kwargs: None,
        )

    def tearDown(self):
        result_cache.get_result_cache_store = self._old_get_store
        nodes_image.JimengGenerationExecutor.run_parallel_requests = self._old_run_parallel
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def _fake_run_parallel_requests(self, generation_count, request_func, **kwargs):
        self.request_count.append(generation_count)
        rng = numpy.random.RandomState(7)
        arr = (rng.rand(16, 12, 3) * 255).astype(numpy.uint8)
        tensor = nodes_image.torch.from_numpy(arr.astype(numpy.float32) / 255.0)[None]
        metadata = [
            {
                "batch_index": 0,
                "model": "doubao-seedream-4-0-250828",
                "created": 1700000000,
                "images": [{"index": 1, "source": "b64_json"}],
            }
        ]
        return [tensor], metadata

    def _run_node(self, **overrides):
        defaults = dict(
            client=self.fake_client,
            model_version="doubao-seedream-4.0",
            prompt="a cat",
            size="2K (adaptive)",
            width=2048,
            height=2048,
            seed=42,
            use_result_cache=True,
            enable_group_generation=False,
            max_images=1,
            generation_count=1,
            watermark=False,
            thinking=True,
        )
        defaults.update(overrides)

        async def _go():
            return await nodes_image.JimengSeedream4.execute(**defaults)

        return asyncio.run(_go())

    def test_same_input_hits_cache_without_new_request(self):
        out1 = self._run_node()
        self.assertEqual(len(self.request_count), 1)

        out2 = self._run_node()
        self.assertEqual(len(self.request_count), 1, "second run must not call the API again")
        self.assertEqual(out1.args[1], out2.args[1])
        numpy.testing.assert_allclose(
            out1.args[0].numpy(), out2.args[0].numpy(), atol=0.0
        )

    def test_different_seed_misses(self):
        self._run_node()
        self._run_node(seed=43)
        self.assertEqual(len(self.request_count), 2)

    def test_toggle_off_always_requests(self):
        self._run_node(use_result_cache=False)
        self._run_node(use_result_cache=False)
        self.assertEqual(len(self.request_count), 2)

    def test_cache_survives_store_reload(self):
        self._run_node()
        self.store = result_cache.ResultCacheStore(self.cache_dir)
        self._run_node()
        self.assertEqual(len(self.request_count), 1)


if __name__ == "__main__":
    unittest.main()
