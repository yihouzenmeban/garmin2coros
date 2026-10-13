import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class SyncStartupTest(unittest.TestCase):
    def test_sync_entrypoints_defer_optional_upload_and_browser_dependencies(self):
        code = '''
import importlib, importlib.abc, json, sys
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        optional = {"oss2", "boto3"}
        if sys.argv[1].startswith("scripts.coros."):
            optional.add("garth")
        if fullname.split(".")[0] in optional or fullname.endswith("generate_garth_token_browser"):
            raise RuntimeError("Optional dependency loaded before it is needed: " + fullname)
sys.meta_path.insert(0, Guard())
importlib.import_module(sys.argv[1])
print(json.dumps({"ready": True}))
'''
        env = {**os.environ, "GARTH_TELEMETRY_ENABLED": "false", "GARTH_TELEMETRY_SEND_TO_LOGFIRE": "false"}
        for module in ("scripts.garmin.garmin_sync_coros", "scripts.coros.coros_sync_garmin"):
            with self.subTest(module=module):
                result = subprocess.run([sys.executable, "-c", code, module], cwd=PROJECT_ROOT, env=env,
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(json.loads(result.stdout)["ready"])

    def test_storage_creation_loads_only_the_selected_region_sdk(self):
        code = '''
import importlib.abc, json, sys, types
region = int(sys.argv[1])
selected = "ali" if region == 2 else "aws"
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {"oss.ali_oss_client", "oss.aws_oss_client"}:
            raise RuntimeError("Unselected storage SDK loaded: " + fullname)
sys.meta_path.insert(0, Guard())
from scripts.garmin import garmin_sync_coros
name = "oss." + selected + "_oss_client"
module = types.ModuleType(name)
class Storage:
    def __init__(self, **kwargs):
        self.options = kwargs
setattr(module, "AliOssClient" if selected == "ali" else "AwsOssClient", Storage)
sys.modules[name] = module
client = garmin_sync_coros.create_storage_client(types.SimpleNamespace(regionId=region, accessToken="synthetic-token"))
print(json.dumps(client.options))
'''
        for region in (1, 2, 3):
            with self.subTest(region=region):
                result = subprocess.run([sys.executable, "-c", code, str(region)], cwd=PROJECT_ROOT,
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                options = json.loads(result.stdout)
                self.assertEqual(options["region_id"], region)
                self.assertEqual(options["service"], "aliyun" if region == 2 else "aws")
