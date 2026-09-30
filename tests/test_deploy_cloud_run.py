"""Execute deployment orchestration against a local fake gcloud, never the cloud."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


FAKE_GCLOUD = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
a=sys.argv[1:]
root=pathlib.Path(os.environ['DEPLOY_TEST_STATE'])
with (root/'calls.jsonl').open('a') as f: f.write(json.dumps(a)+'\n')
def option(name):
    for i,v in enumerate(a):
        if v == name: return a[i+1]
        if v.startswith(name+'='): return v.split('=',1)[1]
    return None
statefile=root/'state.json'
state=json.loads(statefile.read_text()) if statefile.exists() else {}
if a[:3]==['secrets','versions','access']:
    print('postgresql://fixture:private@cloud.example/db'+os.environ.get('DEPLOY_TEST_DB_QUERY','?connection_limit=3&pool_timeout=30'))
elif a[:4]==['artifacts','docker','images','describe']:
    print('sha256:'+'a'*64)
elif a[:2]==['run','deploy'] or a[:3]==['run','services','update']:
    service=a[2] if a[1]=='deploy' else a[3]
    previous=state.get(service,{})
    revision=service+'-'+str(previous.get('counter',0)+1)
    tag=option('--tag')
    state[service]={'counter':previous.get('counter',0)+1,'revision':revision,'tag':tag}
    statefile.write_text(json.dumps(state))
    print(revision)
elif a[:3]==['run','services','describe']:
    service=a[3]
    if option('--format')=='json':
        s=state[service]
        print(json.dumps({'status':{'traffic':[{'revisionName':s['revision'],'tag':s['tag'],'url':'https://'+s['tag']+'---'+service+'.example'}]}}))
    elif 'serviceAccountName' in option('--format'):
        print('runtime@fixture.iam.gserviceaccount.com')
    else: print('https://'+service+'.example')
elif a[:2]==['projects','describe']:
    print('123456')
'''


class DeploymentOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        shutil.copyfile(Path(__file__).resolve().parents[1] / "deploy_cloud_run.sh", self.root / "deploy_cloud_run.sh")
        (self.root / "litellm_config.yaml").write_text("model_list: []\n")
        (self.root / ".gitignore").write_text("bin/\nstate/\nevidence/\n")
        for directory in ("bin", "state", "evidence"):
            (self.root / directory).mkdir()
        fake = self.root / "bin/gcloud"
        fake.write_text(FAKE_GCLOUD)
        fake.chmod(0o755)
        # The pinned service image intentionally has no Git/Perl utilities.
        # Keep orchestration tests hermetic there as well as on a maintainer Mac.
        fixtures = {
            "git": "import sys; sys.stdout.buffer.write((b'4'*40+b'\\n') if sys.argv[1]=='rev-parse' else b'deploy_cloud_run.sh\\0litellm_config.yaml\\0.gitignore\\0')",
            "shasum": "import hashlib,pathlib,sys; data=pathlib.Path(sys.argv[3]).read_bytes() if len(sys.argv)>3 else sys.stdin.buffer.read(); print(hashlib.sha256(data).hexdigest()+'  fixture')",
        }
        for name, body in fixtures.items():
            command = self.root / "bin" / name
            command.write_text("#!/usr/bin/env python3\n" + body + "\n")
            command.chmod(0o755)
        self.env = {"PATH": str(self.root / "bin") + os.pathsep + os.environ["PATH"],
                    "HOME": os.environ.get("HOME", str(self.root)),
                    "DEPLOY_TEST_STATE": str(self.root / "state"),
                    "RELEASE_MANIFEST_DIR": str(self.root / "evidence")}

    def tearDown(self):
        self.temp.cleanup()

    def run_deploy(self, *args, **env):
        return subprocess.run(["bash", "deploy_cloud_run.sh", "--tag", "fixture-release", *args],
                              cwd=self.root, env={**self.env, **env}, text=True, capture_output=True)

    def calls(self):
        return [json.loads(line) for line in (self.root / "state/calls.jsonl").read_text().splitlines()]

    def manifest(self):
        return json.loads(next((self.root / "evidence").glob("*.json")).read_text())

    def test_serving_candidate_includes_callback_and_final_configuration_without_traffic(self):
        result = self.run_deploy("--candidate-only")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        deploys = [call for call in calls if call[:2] == ["run", "deploy"]]
        self.assertEqual([call[2] for call in deploys], ["ai-gateway-proxy", "ai-gateway-callbacks"])
        for call in deploys:
            self.assertIn("--no-traffic", call)
            self.assertIn("@sha256:", call[call.index("--image")+1])
        update = next(call for call in calls if call[:3] == ["run", "services", "update"])
        self.assertIn("--no-traffic", update)
        manifest = self.manifest()
        self.assertEqual(manifest["gateway_revision"], "ai-gateway-proxy-2")
        self.assertEqual(manifest["callback_revision"], "ai-gateway-callbacks-1")
        self.assertIn(manifest["callback_candidate_url"], update[update.index("--update-env-vars")+1])
        self.assertIn(manifest["gateway_candidate_url"], deploys[1][deploys[1].index("--update-env-vars")+1])
        self.assertFalse(any(call[:3] == ["run", "services", "update-traffic"] for call in calls))
        self.assertFalse(any(call[:2] == ["scheduler", "jobs"] for call in calls))
        self.assertIn("callback first", result.stdout)
        self.assertNotIn("fixture:private@", result.stdout + result.stderr)
        self.assertEqual(manifest["source_identity_scope"], "build-checkout")

    def test_migration_candidate_is_separate_and_never_deploys_callback(self):
        result = self.run_deploy("--candidate-only", "--run-migrations")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        deploys = [call for call in calls if call[:2] == ["run", "deploy"]]
        self.assertEqual(len(deploys), 1)
        self.assertEqual(deploys[0][deploys[0].index("--max-instances")+1], "1")
        self.assertIn("RUN_LITELLM_MIGRATIONS=true", deploys[0][deploys[0].index("--update-env-vars")+1])
        manifest = self.manifest()
        self.assertEqual(manifest["stage"], "migration")
        self.assertEqual(manifest["callback_revision"], "")
        self.assertTrue(manifest["gateway_candidate_url"].startswith("https://migration-"))
        self.assertIn("never promote", result.stdout)

    def test_immutable_image_reuse_does_not_rebuild(self):
        image = "us-central1-docker.pkg.dev/fixture/images/gateway@sha256:" + "b"*64
        result = self.run_deploy("--candidate-only", "--image-uri", image)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(call[:2] == ["builds", "submit"] for call in self.calls()))
        self.assertEqual(self.manifest()["image_uri"], image)
        self.assertEqual(len(self.manifest()["source_state_sha256"]), 64)
        self.assertEqual(len(self.manifest()["config_sha256"]), 64)
        self.assertEqual(self.manifest()["source_identity_scope"], "deployment-checkout-only")

    def test_serving_reuses_exact_migration_image_provenance(self):
        migration = self.run_deploy("--candidate-only", "--run-migrations")
        self.assertEqual(migration.returncode, 0, migration.stderr)
        original = self.manifest()
        (self.root / "litellm_config.yaml").write_text("model_list: [changed-locally]\n")
        serving = self.run_deploy("--candidate-only", "--image-uri", original["image_uri"])
        self.assertEqual(serving.returncode, 0, serving.stderr)
        current = json.loads(next((self.root / "evidence").glob("*-serving.json")).read_text())
        self.assertEqual(current["source_identity_scope"], "reused-migration-image")
        self.assertEqual(current["config_sha256"], original["config_sha256"])
        self.assertEqual(current["source_state_sha256"], original["source_state_sha256"])

    def test_mutable_reuse_and_migration_promotion_are_refused_before_cloud_calls(self):
        for args in (("--image-uri", "registry.example/image:latest"), ("--run-migrations",)):
            with self.subTest(args=args):
                result = self.run_deploy(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.root / "state/calls.jsonl").exists())

    def test_combined_pool_budget_and_concurrency_are_enforced_before_build(self):
        result = self.run_deploy("--candidate-only", "--max-instances", "1", "--callback-max-instances", "2",
                                "--concurrency", "8", "--callback-concurrency", "4",
                                "--db-overlap-connections", "2", "--db-connection-budget", "11")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.manifest()["db_connection_envelope"], "11")
        deploys = [call for call in self.calls() if call[:2] == ["run", "deploy"]]
        self.assertEqual([call[call.index("--concurrency")+1] for call in deploys], ["8", "4"])

    def test_insufficient_budget_or_unknown_prisma_limit_never_builds(self):
        for query in ("?connection_limit=3", ""):
            with self.subTest(query=query):
                result = self.run_deploy("--candidate-only", "--db-connection-budget", "6", DEPLOY_TEST_DB_QUERY=query)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(call[:2] == ["builds", "submit"] for call in self.calls()))

    def test_normal_deploy_retains_service_routing_and_scheduler_configuration(self):
        result = self.run_deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        deploys = [call for call in calls if call[:2] == ["run", "deploy"]]
        self.assertTrue(all("--no-traffic" not in call for call in deploys))
        callback_env = deploys[1][deploys[1].index("--update-env-vars")+1]
        self.assertIn("GENERATION_POLL_TARGET_URL=https://ai-gateway-proxy.example,", callback_env)
        self.assertTrue(any(call[:3] == ["scheduler", "jobs", "update"] for call in calls))


if __name__ == "__main__":
    unittest.main()
