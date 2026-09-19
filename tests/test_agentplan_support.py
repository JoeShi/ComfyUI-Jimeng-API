import importlib
import json
import os
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace


PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _install_stub(name, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules.setdefault(name, module)
    return module


def _install_heavy_dependency_stubs():
    """
    为无法在纯单元环境安装的重依赖提供最小桩模块，
    使 nodes_shared 可脱离 ComfyUI / torch / Ark SDK 导入。
    """
    _install_stub("folder_paths")
    _install_stub("numpy")
    _install_stub("cv2")
    _install_stub("requests")

    torch_module = _install_stub("torch")
    torch_module.ones = lambda *args, **kwargs: None
    torch_module.Tensor = type("Tensor", (), {})
    torch_module.cat = lambda *args, **kwargs: None
    nn_module = _install_stub("torch.nn")
    functional_module = _install_stub("torch.nn.functional")
    functional_module.interpolate = lambda *args, **kwargs: None
    nn_module.functional = functional_module
    torch_module.nn = nn_module

    pil_module = _install_stub("PIL")
    pil_image_module = _install_stub("PIL.Image")
    pil_image_module.fromarray = lambda *args, **kwargs: None
    pil_module.Image = pil_image_module

    class _FakeArk:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    ark_sdk = _install_stub("volcenginesdkarkruntime", Ark=_FakeArk)

    comfy_api = _install_stub("comfy_api")

    class _InputSpec:
        def __init__(self, input_id, **kwargs):
            self.id = input_id
            self.__dict__.update(kwargs)

    class _Combo:
        @staticmethod
        def Input(input_id, options=None, default=None, **kwargs):
            return _InputSpec(input_id, options=options, default=default, **kwargs)

    class _String:
        @staticmethod
        def Input(input_id, **kwargs):
            return _InputSpec(input_id, **kwargs)

    class _CustomType:
        def __init__(self, type_name):
            self.type_name = type_name

        def Input(self, input_id=None, **kwargs):
            return _InputSpec(input_id, **kwargs)

        Output = Input

    comfy_api.latest = _install_stub("comfy_api.latest")
    comfy_api.latest.io = SimpleNamespace(
        ComfyNode=type("ComfyNode", (), {}),
        Schema=lambda **kwargs: SimpleNamespace(**kwargs),
        String=_String,
        Combo=_Combo,
        Custom=lambda type_name: _CustomType(type_name),
        NodeOutput=lambda value, **kwargs: SimpleNamespace(value=value, **kwargs),
        Hidden=SimpleNamespace(
            unique_id="unique_id",
            prompt="prompt",
            api_key_comfy_org="api_key_comfy_org",
        ),
        Image=SimpleNamespace(
            Output=lambda **kwargs: SimpleNamespace(**kwargs),
            Input=lambda **kwargs: SimpleNamespace(**kwargs),
        ),
    )

    return ark_sdk


def _load_plugin_modules():
    package_name = "jimeng_agentplan_test"
    if package_name not in sys.modules:
        package = types.ModuleType(package_name)
        package.__path__ = [PLUGIN_ROOT]
        sys.modules[package_name] = package

    nodes_shared = importlib.import_module(
        f"{package_name}.nodes.nodes_shared"
    )
    models_config = importlib.import_module(
        f"{package_name}.nodes.models_config"
    )
    constants = importlib.import_module(f"{package_name}.nodes.constants")
    return nodes_shared, models_config, constants


_install_heavy_dependency_stubs()
nodes_shared, models_config, constants = _load_plugin_modules()


class AgentPlanConstantTests(unittest.TestCase):
    def test_agentplan_base_url_and_options(self):
        self.assertEqual(
            constants.JIMENG_API_BASE_URL,
            "https://ark.cn-beijing.volces.com/api/v3",
        )
        self.assertEqual(
            constants.AGENTPLAN_API_BASE_URL,
            "https://ark.cn-beijing.volces.com/api/plan/v3",
        )
        self.assertNotEqual(
            constants.AGENTPLAN_API_BASE_URL, constants.JIMENG_API_BASE_URL
        )
        self.assertEqual(constants.AUTH_MODE_OPTIONS, ["ark", "agentplan"])
        self.assertEqual(constants.AUTH_MODE_ARK, "ark")
        self.assertEqual(constants.AUTH_MODE_AGENTPLAN, "agentplan")

    def test_resolve_base_url(self):
        self.assertEqual(
            nodes_shared.resolve_base_url("agentplan"),
            constants.AGENTPLAN_API_BASE_URL,
        )
        self.assertEqual(
            nodes_shared.resolve_base_url("ark"),
            constants.JIMENG_API_BASE_URL,
        )
        self.assertEqual(
            nodes_shared.resolve_base_url(None),
            constants.JIMENG_API_BASE_URL,
        )
        self.assertEqual(
            nodes_shared.resolve_base_url("unknown-mode"),
            constants.JIMENG_API_BASE_URL,
        )

    def test_normalize_auth_mode(self):
        self.assertEqual(nodes_shared.normalize_auth_mode("agentplan"), "agentplan")
        self.assertEqual(nodes_shared.normalize_auth_mode("ark"), "ark")
        self.assertEqual(nodes_shared.normalize_auth_mode("bad"), "ark")
        self.assertEqual(nodes_shared.normalize_auth_mode(None), "ark")


class AgentPlanModelListTests(unittest.TestCase):
    def test_known_plan_visual_models(self):
        self.assertTrue(
            models_config.is_agentplan_supported_visual_model(
                models_config.SEEDREAM_5_MODEL_MAP["doubao-seedream-5.0-lite"]
            )
        )
        self.assertTrue(
            models_config.is_agentplan_supported_visual_model(
                models_config.SEEDREAM_5_MODEL_MAP["doubao-seedream-5.0-pro"]
            )
        )
        for model in (
            "doubao-seedance-2-0",
            "doubao-seedance-2-0-fast",
            "doubao-seedance-2-0-mini",
            "doubao-seedance-2-5",
        ):
            self.assertTrue(
                models_config.is_agentplan_supported_visual_model(
                    models_config.VIDEO_MODEL_MAP[model]
                )
            )

    def test_non_plan_models_are_reported(self):
        self.assertFalse(
            models_config.is_agentplan_supported_visual_model(
                models_config.SEEDREAM_3_MODELS["t2i"]
            )
        )
        self.assertFalse(
            models_config.is_agentplan_supported_visual_model(
                models_config.SEEDREAM_4_MODEL_MAP["doubao-seedream-4.0"]
            )
        )
        self.assertFalse(
            models_config.is_agentplan_supported_visual_model("doubao-seed-2-1-pro-260628")
        )


class ApiKeyStoreAuthModeTests(unittest.TestCase):
    def _temp_store(self, items=None):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(path)
        store = nodes_shared.ApiKeyStore(path)
        if items is not None:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(items, f)
        return store, path

    def test_upsert_persists_auth_mode(self):
        store, path = self._temp_store()
        try:
            self.assertTrue(store.upsert("PlanKey", "plan-secret", auth_mode="agentplan"))
            self.assertTrue(store.upsert("ArkKey", "ark-secret"))
            store.load()

            plan_entry = store.find_entry("PlanKey")
            self.assertEqual(plan_entry["apiKey"], "plan-secret")
            self.assertEqual(plan_entry["authMode"], "agentplan")

            ark_entry = store.find_entry("ArkKey")
            self.assertEqual(ark_entry["authMode"], "ark")
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_legacy_entries_keep_null_auth_mode(self):
        store, path = self._temp_store(
            [{"customName": "Legacy", "apiKey": "legacy-secret"}]
        )
        try:
            store.load()
            entry = store.find_entry("Legacy")
            self.assertEqual(entry["apiKey"], "legacy-secret")
            self.assertIsNone(entry["authMode"])
            self.assertEqual(store.find_api_key("Legacy"), "legacy-secret")
        finally:
            os.remove(path)

    def test_invalid_auth_mode_becomes_null(self):
        store, path = self._temp_store(
            [
                {
                    "customName": "Weird",
                    "apiKey": "secret",
                    "authMode": "coding-plan",
                }
            ]
        )
        try:
            store.load()
            self.assertIsNone(store.find_entry("Weird")["authMode"])
        finally:
            os.remove(path)


class ValidateApiKeyTests(unittest.TestCase):
    def _with_fake_requests(self, status_code):
        captured = {}

        def fake_get(url, headers=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers
            return SimpleNamespace(status_code=status_code)

        fake_requests = SimpleNamespace(get=fake_get)
        original = nodes_shared.requests
        nodes_shared.requests = fake_requests
        self.addCleanup(setattr, nodes_shared, "requests", original)
        return captured

    def test_validates_against_agentplan_base_url(self):
        captured = self._with_fake_requests(200)
        self.assertTrue(
            nodes_shared.validate_api_key(
                "plan-key", base_url=constants.AGENTPLAN_API_BASE_URL
            )
        )
        self.assertEqual(captured["url"], constants.AGENTPLAN_API_BASE_URL)
        self.assertEqual(captured["headers"]["Authorization"], "Bearer plan-key")

    def test_401_is_rejected(self):
        self._with_fake_requests(401)
        self.assertFalse(
            nodes_shared.validate_api_key(
                "bad-key", base_url=constants.AGENTPLAN_API_BASE_URL
            )
        )

    def test_defaults_to_ark_base_url(self):
        captured = self._with_fake_requests(200)
        nodes_shared.validate_api_key("ark-key")
        self.assertEqual(captured["url"], constants.JIMENG_API_BASE_URL)


class _ArkRecorder:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        _ArkRecorder.instances.append(self)


class JimengAPIClientExecuteTests(unittest.TestCase):
    def _restore(self):
        nodes_shared.API_KEY_STORE = self.original_store
        nodes_shared.Ark = self.original_ark
        nodes_shared.validate_api_key = self.original_validate
        if os.path.exists(self.temp_store_path):
            os.remove(self.temp_store_path)

    def setUp(self):
        _ArkRecorder.instances = []

        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(path)
        self.temp_store = nodes_shared.ApiKeyStore(path)
        self.temp_store_path = path

        self.original_store = nodes_shared.API_KEY_STORE
        self.original_ark = nodes_shared.Ark
        self.original_validate = nodes_shared.validate_api_key
        nodes_shared.API_KEY_STORE = self.temp_store
        nodes_shared.Ark = _ArkRecorder
        nodes_shared.validate_api_key = lambda *args, **kwargs: True
        self.addCleanup(self._restore)

    def _execute(self, **kwargs):
        return nodes_shared.JimengAPIClient.execute(**kwargs)

    def test_custom_key_with_agentplan_uses_plan_base_url_and_saves_mode(self):
        self.temp_store.upsert = lambda *args, **kwargs: True
        result = self._execute(
            key_name="Custom",
            new_api_key="plan-secret",
            new_key_name="MyPlanKey",
            auth_mode="agentplan",
        )

        client = result.value
        self.assertEqual(len(_ArkRecorder.instances), 1)
        self.assertEqual(
            _ArkRecorder.instances[0].kwargs["base_url"],
            constants.AGENTPLAN_API_BASE_URL,
        )
        self.assertEqual(_ArkRecorder.instances[0].kwargs["api_key"], "plan-secret")
        self.assertEqual(client.auth_mode, "agentplan")
        self.assertEqual(client.base_url, constants.AGENTPLAN_API_BASE_URL)

    def test_saved_agentplan_key_overrides_node_selection(self):
        with open(self.temp_store_path, "w", encoding="utf-8") as f:
            json.dump(
                [
                    {
                        "customName": "PlanKey",
                        "apiKey": "plan-secret",
                        "authMode": "agentplan",
                    }
                ],
                f,
            )
        self.temp_store.load()

        result = self._execute(key_name="PlanKey", auth_mode="ark")

        client = result.value
        self.assertEqual(client.auth_mode, "agentplan")
        self.assertEqual(
            _ArkRecorder.instances[0].kwargs["base_url"],
            constants.AGENTPLAN_API_BASE_URL,
        )

    def test_legacy_saved_key_uses_node_selection(self):
        with open(self.temp_store_path, "w", encoding="utf-8") as f:
            json.dump(
                [{"customName": "LegacyKey", "apiKey": "legacy-secret"}], f
            )
        self.temp_store.load()

        result = self._execute(key_name="LegacyKey", auth_mode="agentplan")

        client = result.value
        self.assertEqual(client.auth_mode, "agentplan")
        self.assertEqual(
            _ArkRecorder.instances[0].kwargs["base_url"],
            constants.AGENTPLAN_API_BASE_URL,
        )
        self.assertEqual(
            _ArkRecorder.instances[0].kwargs["api_key"], "legacy-secret"
        )

    def test_ark_mode_uses_default_base_url(self):
        with open(self.temp_store_path, "w", encoding="utf-8") as f:
            json.dump(
                [
                    {
                        "customName": "ArkKey",
                        "apiKey": "ark-secret",
                        "authMode": "ark",
                    }
                ],
                f,
            )
        self.temp_store.load()

        result = self._execute(key_name="ArkKey", auth_mode="ark")

        client = result.value
        self.assertEqual(client.auth_mode, "ark")
        self.assertEqual(
            _ArkRecorder.instances[0].kwargs["base_url"],
            constants.JIMENG_API_BASE_URL,
        )

    def test_missing_key_raises(self):
        with self.assertRaises(Exception):
            self._execute(key_name="NotExists", auth_mode="agentplan")

    def test_custom_agentplan_key_validates_against_plan_url(self):
        captured = {}

        def fake_validate(api_key, base_url=constants.JIMENG_API_BASE_URL):
            captured["api_key"] = api_key
            captured["base_url"] = base_url
            return True

        original = nodes_shared.validate_api_key
        nodes_shared.validate_api_key = fake_validate
        self.addCleanup(setattr, nodes_shared, "validate_api_key", original)
        self.temp_store.upsert = lambda *args, **kwargs: True

        self._execute(
            key_name="Custom",
            new_api_key="plan-secret",
            new_key_name="",
            auth_mode="agentplan",
        )

        self.assertEqual(captured["api_key"], "plan-secret")
        self.assertEqual(captured["base_url"], constants.AGENTPLAN_API_BASE_URL)


class JimengClientsQuotaWarningTests(unittest.TestCase):
    def _capture_log(self):
        recorded = []

        def fake_log(key, **kwargs):
            recorded.append((key, kwargs))

        original = nodes_shared.log_msg
        nodes_shared.log_msg = fake_log
        self.addCleanup(setattr, nodes_shared, "log_msg", original)
        return recorded

    def test_agentplan_mode_warns_for_non_plan_model(self):
        recorded = self._capture_log()
        client = nodes_shared.JimengClients(
            SimpleNamespace(), None, "agentplan"
        )
        client.check_quota("doubao-seedream-3-0-t2i-250415", 1)
        self.assertTrue(
            any(key == "warn_agentplan_model_unsupported" for key, _ in recorded)
        )

    def test_agentplan_mode_does_not_warn_for_plan_model(self):
        recorded = self._capture_log()
        client = nodes_shared.JimengClients(
            SimpleNamespace(), None, "agentplan"
        )
        client.check_quota("doubao-seedream-5-0-260128", 1)
        self.assertFalse(
            any(key == "warn_agentplan_model_unsupported" for key, _ in recorded)
        )

    def test_ark_mode_never_warns_about_plan_models(self):
        recorded = self._capture_log()
        client = nodes_shared.JimengClients(SimpleNamespace(), None, "ark")
        client.check_quota("doubao-seedream-3-0-t2i-250415", 1)
        self.assertEqual(recorded, [])


class ClientSchemaTests(unittest.TestCase):
    def test_schema_declares_auth_mode_combo(self):
        schema = nodes_shared.JimengAPIClient.define_schema()
        auth_mode_input = next(
            item for item in schema.inputs if item.id == "auth_mode"
        )
        self.assertEqual(auth_mode_input.options, ["ark", "agentplan"])
        self.assertEqual(auth_mode_input.default, "ark")

        input_ids = [item.id for item in schema.inputs]
        self.assertEqual(
            input_ids, ["new_api_key", "new_key_name", "key_name", "auth_mode"]
        )


if __name__ == "__main__":
    unittest.main()
