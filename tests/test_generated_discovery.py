from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from collections.abc import Iterator
from os import PathLike
from pathlib import Path
from unittest.mock import patch

from vibe_loop.generated_discovery import (
    EvidenceLimits,
    collect_generated_discovery_evidence,
    redact_evidence_text,
)


class GeneratedDiscoveryEvidenceTests(unittest.TestCase):
    def test_collects_allowed_task_evidence_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "docs").mkdir()
            (repo / "docs" / "ROADMAP.md").write_text("roadmap\n", encoding="utf-8")
            (repo / "PLAN.md").write_text("plan\n", encoding="utf-8")
            (repo / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

            bundle = collect_generated_discovery_evidence(repo)

        self.assertEqual(
            [file.path for file in bundle.files],
            ["PLAN.md", "docs/ROADMAP.md", "pyproject.toml"],
        )
        self.assertEqual(bundle.manifest_json()["total_bytes"], 23)
        for manifest_file in bundle.manifest_json()["files"]:
            self.assertNotIn("mtime_ns", manifest_file)
        for prompt_file in bundle.prompt_input_json()["files"]:
            self.assertNotIn("mtime_ns", prompt_file)

    def test_skips_unsupported_binary_and_ignored_state_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "PLAN.md").write_text("plan\n", encoding="utf-8")
            (repo / "script.py").write_text("print('not task evidence')\n")
            (repo / "binary.md").write_bytes(b"\x00\x01not text")
            (repo / ".vibe-loop").mkdir()
            (repo / ".vibe-loop" / "generated-task-source.json").write_text(
                "{}\n",
                encoding="utf-8",
            )
            (repo / "build").mkdir()
            (repo / "build" / "ROADMAP.md").write_text("ignored\n", encoding="utf-8")

            bundle = collect_generated_discovery_evidence(repo)

        self.assertEqual([file.path for file in bundle.files], ["PLAN.md"])
        skipped = {(item.path, item.reason) for item in bundle.skipped}
        self.assertIn(("script.py", "unsupported_file_type"), skipped)
        self.assertIn(("binary.md", "binary_file"), skipped)
        self.assertIn((".vibe-loop", "state_directory"), skipped)
        self.assertIn(("build", "ignored_directory"), skipped)

    def test_uses_configured_state_directory_skip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            state = repo / ".state" / "vibe-loop"
            state.mkdir(parents=True)
            (state / "PLAN.md").write_text("cached\n", encoding="utf-8")
            (repo / "PLAN.md").write_text("plan\n", encoding="utf-8")

            bundle = collect_generated_discovery_evidence(
                repo,
                state_dir=".state/vibe-loop",
            )

        self.assertEqual([file.path for file in bundle.files], ["PLAN.md"])
        self.assertIn(
            (".state/vibe-loop", "state_directory"),
            {(item.path, item.reason) for item in bundle.skipped},
        )

    def test_skips_env_key_files_and_credential_directories_without_reading_them(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory).resolve()
            (repo / "PLAN.md").write_text("plan\n", encoding="utf-8")
            (repo / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
            (repo / ".env.production").write_text("TOKEN=secret\n", encoding="utf-8")
            (repo / ".env.hunter2").write_text("TOKEN=secret\n", encoding="utf-8")
            (repo / ".env%2Eproduction").write_text(
                "TOKEN=encoded-secret\n",
                encoding="utf-8",
            )
            (repo / ".netrc%2Ebackup.md").write_text(
                "machine internal-host\n",
                encoding="utf-8",
            )
            (repo / ".netrc%2Finternal-host.md").write_text(
                "login encoded-netrc\n",
                encoding="utf-8",
            )
            (repo / "deploy.key").write_text("private key\n", encoding="utf-8")
            (repo / "client-secret.json").write_text('{"secret": true}\n')
            (repo / ".ssh").mkdir()
            (repo / ".ssh" / "config").write_text("Host *\n", encoding="utf-8")
            (repo / ".docker").mkdir()
            (repo / ".docker" / "config.json").write_text(
                '{"auth": "registry-secret"}\n',
                encoding="utf-8",
            )
            original_read_bytes = Path.read_bytes
            read_paths: list[str] = []

            def read_bytes(path: Path) -> bytes:
                relative = path.relative_to(repo).as_posix()
                read_paths.append(relative)
                if relative != "PLAN.md":
                    raise AssertionError(f"{relative} should be skipped before read")
                return original_read_bytes(path)

            with patch.object(Path, "read_bytes", read_bytes):
                bundle = collect_generated_discovery_evidence(repo)

        self.assertEqual([file.path for file in bundle.files], ["PLAN.md"])
        self.assertEqual(read_paths, ["PLAN.md"])
        skipped = {(item.path, item.reason) for item in bundle.skipped}
        self.assertIn((".env", "secret_path"), skipped)
        self.assertIn((".env.<redacted>", "secret_path"), skipped)
        self.assertIn((".netrc.<redacted>", "secret_path"), skipped)
        self.assertNotIn("hunter2", str(bundle.manifest_json()))
        self.assertNotIn("internal-host", str(bundle.prompt_input_json()))
        self.assertIn(("deploy.key", "secret_path"), skipped)
        self.assertIn(("client-secret.json", "secret_path"), skipped)
        self.assertIn((".ssh", "secret_directory"), skipped)
        self.assertIn((".docker", "secret_directory"), skipped)

    def test_skips_env_and_virtualenv_directories_without_descending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "PLAN.md").write_text("plan\n", encoding="utf-8")
            for dirname in (
                ".env",
                ".env.production",
                ".env-production",
                ".env_prod",
                ".env%2Eproduction",
                ".env%2Dproduction",
                ".env%5Fprod",
                ".env%2Fhunter2",
                ".direnv",
                ".direnv%2Fcache",
                "venv",
            ):
                env_dir = repo / dirname
                env_dir.mkdir()
                (env_dir / "PLAN.md").write_text("directory secret\n", encoding="utf-8")

            bundle = collect_generated_discovery_evidence(repo)

        self.assertEqual([file.path for file in bundle.files], ["PLAN.md"])
        skipped = {(item.path, item.reason) for item in bundle.skipped}
        self.assertIn((".env", "secret_directory"), skipped)
        self.assertIn((".env.<redacted>", "secret_directory"), skipped)
        self.assertIn((".direnv", "ignored_directory"), skipped)
        self.assertIn((".direnv%2Fcache", "ignored_directory"), skipped)
        self.assertIn(("venv", "ignored_directory"), skipped)
        self.assertNotIn("directory secret", str(bundle.prompt_input_json()))
        self.assertNotIn("hunter2", str(bundle.manifest_json()))

    def test_skips_qualified_secret_directories_without_descending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "PLAN.md").write_text("plan\n", encoding="utf-8")
            for dirname in (
                ".secrets",
                ".credentials",
                "project-secrets",
                "creds",
                "auth",
                "oauth",
                "client-key-data",
                "webhook-url",
                "api%2Dkey",
                ".aws-prod",
                ".aws-hunter2",
                ".ssh-backup",
                "keys",
                "passwords",
                ".password-store",
                "api-key",
                "api_key",
                "api.key",
                "service-account-key",
                "service_account_key",
                "access-key-id",
                "access_key_id",
                "github_pat_actualtoken",
                "id_rsa",
                "id%5Frsa",
                "id_rsa_backup",
                "id_ed25519",
                "ghp_abcd",
                "github_pat_abc",
                "glpat_abc",
                "xoxb_abc",
                "sk-proj-1234567890abcdef",
                "AKIAIOSFODNN7EXAMPLE",
                "ASIAIOSFODNN7EXAMPLE",
                ".gcloud-prod",
                ".gcloud-hunter2",
                ".azure-prod",
                ".kube-prod",
                ".docker-prod",
                ".gnupg-backup",
                "idRsaHunter2",
            ):
                secret_dir = repo / dirname
                secret_dir.mkdir()
                (secret_dir / "PLAN.md").write_text("secret\n", encoding="utf-8")

            bundle = collect_generated_discovery_evidence(repo)

        self.assertEqual([file.path for file in bundle.files], ["PLAN.md"])
        skipped = {(item.path, item.reason) for item in bundle.skipped}
        self.assertIn((".secrets", "secret_directory"), skipped)
        self.assertIn((".credentials", "secret_directory"), skipped)
        self.assertIn(("project-secrets", "secret_directory"), skipped)
        self.assertIn(("creds", "secret_directory"), skipped)
        self.assertIn(("auth", "secret_directory"), skipped)
        self.assertIn(("oauth", "secret_directory"), skipped)
        self.assertIn(("client-key-data", "secret_directory"), skipped)
        self.assertIn(("webhook-url", "secret_directory"), skipped)
        self.assertIn((".aws-<redacted>", "secret_directory"), skipped)
        self.assertIn((".ssh-<redacted>", "secret_directory"), skipped)
        self.assertIn(("keys", "secret_directory"), skipped)
        self.assertIn(("passwords", "secret_directory"), skipped)
        self.assertNotIn("store", str(bundle.manifest_json()))
        self.assertIn(("api-key", "secret_directory"), skipped)
        self.assertIn(("api_key", "secret_directory"), skipped)
        self.assertIn(("api.key", "secret_directory"), skipped)
        self.assertIn(("service-account-key", "secret_directory"), skipped)
        self.assertIn(("service_account_key", "secret_directory"), skipped)
        self.assertIn(("access-key-id", "secret_directory"), skipped)
        self.assertIn(("access_key_id", "secret_directory"), skipped)
        self.assertNotIn("github_pat_actualtoken", str(bundle.manifest_json()))
        self.assertIn(("id_rsa", "secret_directory"), skipped)
        self.assertNotIn("id_rsa_backup", str(bundle.manifest_json()))
        self.assertIn(("id_ed25519", "secret_directory"), skipped)
        self.assertNotIn("ghp_abcd", str(bundle.manifest_json()))
        self.assertNotIn("github_pat_abc", str(bundle.manifest_json()))
        self.assertNotIn("glpat_abc", str(bundle.manifest_json()))
        self.assertNotIn("xoxb_abc", str(bundle.manifest_json()))
        self.assertNotIn("sk-proj-1234567890abcdef", str(bundle.manifest_json()))
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", str(bundle.manifest_json()))
        self.assertNotIn("ASIAIOSFODNN7EXAMPLE", str(bundle.manifest_json()))
        self.assertIn((".gcloud-<redacted>", "secret_directory"), skipped)
        self.assertIn((".azure-<redacted>", "secret_directory"), skipped)
        self.assertIn((".kube-<redacted>", "secret_directory"), skipped)
        self.assertIn((".docker-<redacted>", "secret_directory"), skipped)
        self.assertIn((".gnupg-<redacted>", "secret_directory"), skipped)
        self.assertNotIn("hunter2", str(bundle.manifest_json()))

    def test_skips_secret_like_allowed_file_names_without_reading_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "PLAN.md").write_text("plan\n", encoding="utf-8")
            slack_hook_dir = repo / "hooks.slack.com" / "services" / "T000" / "B000"
            slack_hook_dir.mkdir(parents=True)
            (slack_hook_dir / "XYZ123.md").write_text(
                "slash webhook path material\n",
                encoding="utf-8",
            )
            nested_slack_hook_dir = (
                repo / "docs" / "hooks.slack.com" / "services" / "T000" / "B000"
            )
            nested_slack_hook_dir.mkdir(parents=True)
            (nested_slack_hook_dir / "XYZ123.md").write_text(
                "nested slash webhook path material\n",
                encoding="utf-8",
            )
            mixed_encoded_slack_hook_dir = (
                repo / "hooks%2Eslack%2Ecom" / "services" / "T000" / "B000"
            )
            mixed_encoded_slack_hook_dir.mkdir(parents=True)
            (mixed_encoded_slack_hook_dir / "XYZ123.md").write_text(
                "mixed encoded webhook path material\n",
                encoding="utf-8",
            )
            escaped_slack_hook_dir = (
                repo / "hooks.slack.com\\" / "services\\" / "T000\\" / "B000\\"
            )
            escaped_slack_hook_dir.mkdir(parents=True)
            (escaped_slack_hook_dir / "XYZ123.md").write_text(
                "escaped webhook path material\n",
                encoding="utf-8",
            )
            escaped_dot_slack_hook_dir = (
                repo / "hooks\\.slack\\.com" / "services" / "T000" / "B000"
            )
            escaped_dot_slack_hook_dir.mkdir(parents=True)
            (escaped_dot_slack_hook_dir / "README.md").write_text(
                "escaped dot webhook path material\n",
                encoding="utf-8",
            )
            escaped_encoded_host_slack_hook_dir = (
                repo / "hooks%2Eslack%2Ecom\\" / "services\\" / "T333\\" / "B444\\"
            )
            escaped_encoded_host_slack_hook_dir.mkdir(parents=True)
            (escaped_encoded_host_slack_hook_dir / "MIXED.md").write_text(
                "escaped encoded host webhook path material\n",
                encoding="utf-8",
            )
            escaped_dotted_slack_hook_dir = (
                repo / "hooks%2Eslack%2Ecom\\" / "services\\" / "T000\\" / "B000\\"
            )
            escaped_dotted_slack_hook_dir.mkdir(parents=True)
            (escaped_dotted_slack_hook_dir / "abc.def.md").write_text(
                "escaped dotted webhook path material\n",
                encoding="utf-8",
            )
            escaped_double_encoded_host_slack_hook_dir = (
                repo / "hooks%252Eslack%252Ecom\\" / "services\\" / "T555\\" / "B666\\"
            )
            escaped_double_encoded_host_slack_hook_dir.mkdir(parents=True)
            (escaped_double_encoded_host_slack_hook_dir / "DOUBLE.md").write_text(
                "escaped double encoded host webhook path material\n",
                encoding="utf-8",
            )
            for filename in (
                "credential.json",
                "creds.json",
                "github_pat.txt",
                "gho.txt",
                "ghp_actualtoken.txt",
                "ghp.txt",
                "ghr.txt",
                "ghs.txt",
                "ghu.txt",
                "glpat.txt",
                "id_ecdsa.json",
                "id_ed25519.md",
                "id%5Frsa.txt",
                "id_rsa.txt",
                "passwords.txt",
                "passwd.txt",
                "hunter2-passwd.md",
                "privateKey.txt",
                "private-key-prodsecret.pem",
                "id_rsa_backupsecret.txt",
                "private_key_backupsecret.txt",
                "tokens.md",
                "xoxb.txt",
                "access_key_id.txt",
                "aws-access-key-id.md",
                "aws-access-key-id-AKIAIOSFODNN7EXAMPLE.md",
                "apiKeyProdsecret.yml",
                "accessKeyIdAKIAIOSFODNN7EXAMPLE.txt",
                "ghp-prodsecret.txt",
                "auth-prodsecret.json",
                "credentials-prodsecret.json",
                "oauth-prodsecret.md",
                "password-hunter2.txt",
                "hunter2-password.txt",
                "AKIAIOSFODNN7EXAMPLE-accessKeyId.txt",
                "auth.prodsecret",
                "credentials.prodsecret",
                "oauth.prodsecret",
                "service-account.json",
                "service-account-key.json",
                "service-account-key-mysecret.json",
                "service_account.yaml",
                "api-key.yml",
                "api%2Dkey%2Dabc123.yml",
                "api%2Dkey.yml",
                "api.key.yml",
                "api_key.yml",
                "github%5Fpat%5Fabc123.txt",
                "auth.json",
                "auth_token.md",
                "client-key-data.yaml",
                "client_key_data.yaml",
                "client_secret.json",
                "evidence-hooks.slack.com-services-T000-B000-XYZ123.md",
                "evidence-hooks.slack.com%2Fservices%2FT000%2FB000%2FXYZ123.md",
                "discord.com-api-webhooks-123-abc.def.md",
                "hooks\\\\u002eslack\\\\u002ecom\\\\u002fservices\\\\u002fT000\\\\u002fB000\\\\u002fDOUBLEUNICODE.md",
                "hooks\\u002eslack\\u002ecom\\u002fservices\\u002fT000\\u002fB000\\u002fUNICODE.md",
                "hooks%2Eslack%2Ecom-services-T000-B000-XYZ123.md",
                "hooks%252Eslack%252Ecom-services-T000-B000-XYZ123.md",
                "hooks%2Eslack%2Ecom%2Fservices%2FT000%2FB000%2FXYZ123.md",
                "hooks%252Eslack%252Ecom%252Fservices%252FT000%252FB000%252FXYZ123.md",
                "hooks%25252Eslack%25252Ecom%25252Fservices%25252FT555%25252FB666%25252FTRIPLE.md",
                "hooks.slack.com%252Fservices%252FT000%252FB000%252FXYZ123.md",
                "hooks.slack.com-services-T000-B000-XYZ123.md",
                "hooks.slack.com%2Fservices%2FT000%2FB000%2FXYZ123.md",
                "oauth.md",
                "private_key.txt",
                "slack-webhook-url.txt",
                "service%2Daccount%2Dkey%2Dabc123.json",
                "slack-webhook-url-T000-B000-XYZ123.txt",
                "sk-proj-1234567890abcdef.md",
                "webhook-url.yaml",
                "webhook-url-T000-B000-XYZ123.yaml",
                "AKIAIOSFODNN7EXAMPLE.md",
                "ASIAIOSFODNN7EXAMPLE.md",
            ):
                (repo / filename).write_text("secret\n", encoding="utf-8")

            bundle = collect_generated_discovery_evidence(repo)

        self.assertEqual([file.path for file in bundle.files], ["PLAN.md"])
        skipped = {(item.path, item.reason) for item in bundle.skipped}
        self.assertIn(("credential.json", "secret_path"), skipped)
        self.assertIn(("creds.json", "secret_path"), skipped)
        self.assertIn(("github_pat.txt", "secret_path"), skipped)
        self.assertIn(("gho.txt", "secret_path"), skipped)
        self.assertNotIn("ghp_actualtoken", str(bundle.manifest_json()))
        self.assertIn(("ghp.txt", "secret_path"), skipped)
        self.assertIn(("ghr.txt", "secret_path"), skipped)
        self.assertIn(("ghs.txt", "secret_path"), skipped)
        self.assertIn(("ghu.txt", "secret_path"), skipped)
        self.assertIn(("glpat.txt", "secret_path"), skipped)
        self.assertIn(("id_ecdsa.json", "secret_path"), skipped)
        self.assertIn(("id_ed25519.md", "secret_path"), skipped)
        self.assertIn(("id_rsa.txt", "secret_path"), skipped)
        self.assertIn(("passwords.txt", "secret_path"), skipped)
        self.assertIn(("passwd.txt", "secret_path"), skipped)
        self.assertIn(("privateKey.txt", "secret_path"), skipped)
        self.assertIn(("tokens.md", "secret_path"), skipped)
        self.assertNotIn("xoxb", str(bundle.manifest_json()))
        self.assertIn(("access_key_id.txt", "secret_path"), skipped)
        self.assertIn(("aws-<redacted>", "secret_path"), skipped)
        manifest_text = str(bundle.manifest_json())
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", manifest_text)
        self.assertNotIn("Prodsecret", manifest_text)
        self.assertNotIn("prodsecret", manifest_text)
        self.assertNotIn("hunter2", manifest_text)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", manifest_text)
        self.assertNotIn("prodsecret", manifest_text)
        self.assertNotIn("prodsecret", manifest_text)
        self.assertNotIn("backupsecret", manifest_text)
        self.assertIn(("service-account.json", "secret_path"), skipped)
        self.assertIn(("service-account-key.json", "secret_path"), skipped)
        self.assertNotIn("mysecret", str(bundle.manifest_json()))
        self.assertIn(("service_account.yaml", "secret_path"), skipped)
        self.assertIn(("api-key.yml", "secret_path"), skipped)
        self.assertIn(("<redacted>.yml", "secret_path"), skipped)
        self.assertIn(("api.key.yml", "secret_path"), skipped)
        self.assertIn(("api_key.yml", "secret_path"), skipped)
        self.assertIn(("<redacted>.txt", "secret_path"), skipped)
        self.assertIn(("auth.json", "secret_path"), skipped)
        self.assertIn(("auth_token.md", "secret_path"), skipped)
        self.assertIn(("client-key-data.yaml", "secret_path"), skipped)
        self.assertIn(("client_key_data.yaml", "secret_path"), skipped)
        self.assertIn(("client_secret.json", "secret_path"), skipped)
        self.assertIn(("oauth.md", "secret_path"), skipped)
        self.assertIn(("private_key.txt", "secret_path"), skipped)
        self.assertIn(("<redacted>.json", "secret_path"), skipped)
        self.assertNotIn("T000-B000-XYZ123", manifest_text)
        self.assertNotIn("XYZ123", manifest_text)
        self.assertNotIn("abc123", manifest_text)
        self.assertNotIn("docshooks.slack.com", manifest_text)
        self.assertNotIn("slash webhook path material", str(bundle.prompt_input_json()))
        self.assertNotIn(
            "nested slash webhook path material",
            str(bundle.prompt_input_json()),
        )
        self.assertNotIn(
            "mixed encoded webhook path material",
            str(bundle.prompt_input_json()),
        )
        self.assertNotIn(
            "escaped webhook path material",
            str(bundle.prompt_input_json()),
        )
        self.assertNotIn(
            "escaped dot webhook path material",
            str(bundle.prompt_input_json()),
        )
        self.assertNotIn("T333", manifest_text)
        self.assertNotIn("B444", manifest_text)
        self.assertNotIn("MIXED", manifest_text)
        self.assertNotIn("T555", manifest_text)
        self.assertNotIn("B666", manifest_text)
        self.assertNotIn("TRIPLE", manifest_text)
        self.assertNotIn("abc.def", manifest_text)
        self.assertNotIn("UNICODE", manifest_text)
        self.assertNotIn("DOUBLEUNICODE", manifest_text)
        self.assertNotIn("DOUBLE", manifest_text)
        self.assertNotIn(
            "escaped encoded host webhook path material",
            str(bundle.prompt_input_json()),
        )
        self.assertNotIn(
            "escaped dotted webhook path material",
            str(bundle.prompt_input_json()),
        )
        self.assertNotIn(
            "escaped double encoded host webhook path material",
            str(bundle.prompt_input_json()),
        )
        self.assertNotIn("sk-proj-1234567890abcdef", manifest_text)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", manifest_text)
        self.assertNotIn("ASIAIOSFODNN7EXAMPLE", manifest_text)

    def test_keeps_normal_authentication_and_tokenization_task_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "tokenization").mkdir()
            (repo / "docs" / "webhook").mkdir(parents=True)
            (repo / "authentication.md").write_text(
                "auth task docs\n", encoding="utf-8"
            )
            (repo / "docs" / "webhook" / "PLAN.md").write_text(
                "webhook docs\n",
                encoding="utf-8",
            )
            (repo / "tokenization" / "PLAN.md").write_text(
                "tokenization plan\n",
                encoding="utf-8",
            )
            (repo / "webhook.md").write_text("webhook docs\n", encoding="utf-8")
            (repo / "webhook-integration.md").write_text(
                "webhook integration docs\n",
                encoding="utf-8",
            )
            for filename in (
                "api-key-rotation.markdown",
                "api-key-rotation.md",
                "auth-migration.markdown",
                "auth-migration.md",
                "password-reset.markdown",
                "password-reset.md",
                "token-cleanup.markdown",
                "token-cleanup.md",
            ):
                (repo / filename).write_text("task docs\n", encoding="utf-8")
            for filename in (
                ".api-key-rotation.md",
                ".password-reset.markdown",
                "api-key-rotation..md",
            ):
                (repo / filename).write_text(
                    "hidden task-action file body\n",
                    encoding="utf-8",
                )
            for dirname in (
                "api-key-rotation",
                "auth-migration",
                "password-reset",
                "token-cleanup",
            ):
                task_dir = repo / dirname
                task_dir.mkdir()
                (task_dir / "PLAN.md").write_text("task docs\n", encoding="utf-8")
            for dirname in (
                ".api-key-rotation",
                ".password-reset",
                "api-key-rotation.",
            ):
                secret_dir = repo / dirname
                secret_dir.mkdir()
                (secret_dir / "PLAN.md").write_text(
                    "hidden task-action directory body\n",
                    encoding="utf-8",
                )

            bundle = collect_generated_discovery_evidence(repo)

        self.assertEqual(
            [file.path for file in bundle.files],
            [
                "api-key-rotation.markdown",
                "api-key-rotation.md",
                "api-key-rotation/PLAN.md",
                "auth-migration.markdown",
                "auth-migration.md",
                "auth-migration/PLAN.md",
                "authentication.md",
                "docs/webhook/PLAN.md",
                "password-reset.markdown",
                "password-reset.md",
                "password-reset/PLAN.md",
                "token-cleanup.markdown",
                "token-cleanup.md",
                "token-cleanup/PLAN.md",
                "tokenization/PLAN.md",
                "webhook-integration.md",
                "webhook.md",
            ],
        )
        self.assertNotIn(
            "hidden task-action directory body",
            str(bundle.prompt_input_json()),
        )
        self.assertNotIn(
            "hidden task-action file body",
            str(bundle.prompt_input_json()),
        )

    def test_enforces_per_file_and_total_byte_limits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "a.md").write_text("12345", encoding="utf-8")
            (repo / "b.md").write_text("12345", encoding="utf-8")
            (repo / "c.md").write_text("123456", encoding="utf-8")

            bundle = collect_generated_discovery_evidence(
                repo,
                limits=EvidenceLimits(max_file_bytes=5, max_total_bytes=8),
            )

        self.assertEqual([file.path for file in bundle.files], ["a.md"])
        skipped = {(item.path, item.reason) for item in bundle.skipped}
        self.assertIn(("b.md", "total_size_limit"), skipped)
        self.assertIn(("c.md", "file_too_large"), skipped)

    def test_limits_apply_to_redacted_prompt_content_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "PLAN.md").write_text("TOKEN=a\n", encoding="utf-8")

            bundle = collect_generated_discovery_evidence(
                repo,
                limits=EvidenceLimits(max_file_bytes=100, max_total_bytes=10),
            )

        self.assertEqual(bundle.files, ())
        self.assertIn(
            ("PLAN.md", "total_size_limit"),
            {(item.path, item.reason) for item in bundle.skipped},
        )

    def test_output_limit_is_checked_after_redaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "a.md").write_text("TOKEN=a\n", encoding="utf-8")
            (repo / "b.md").write_text(
                "sk-proj-1234567890abcdef\n",
                encoding="utf-8",
            )

            bundle = collect_generated_discovery_evidence(
                repo,
                limits=EvidenceLimits(max_file_bytes=100, max_total_bytes=40),
            )

        self.assertEqual([file.path for file in bundle.files], ["a.md", "b.md"])
        self.assertIn("<redacted token>", bundle.files[1].content)

    def test_skips_files_over_remaining_raw_budget_without_reading(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "a.md").write_text("12345678", encoding="utf-8")
            (repo / "b.md").write_text("12345", encoding="utf-8")
            original_read_bytes = Path.read_bytes
            read_paths: list[str] = []

            def read_bytes(path: Path) -> bytes:
                read_paths.append(path.name)
                if path.name == "b.md":
                    raise AssertionError("b.md should be skipped before read")
                return original_read_bytes(path)

            with patch.object(Path, "read_bytes", read_bytes):
                bundle = collect_generated_discovery_evidence(
                    repo,
                    limits=EvidenceLimits(max_file_bytes=10, max_total_bytes=10),
                )

        self.assertEqual([file.path for file in bundle.files], ["a.md"])
        self.assertEqual(read_paths, ["a.md"])
        self.assertIn(
            ("b.md", "total_size_limit"),
            {(item.path, item.reason) for item in bundle.skipped},
        )

    def test_skipped_reads_consume_raw_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "a-binary.md").write_bytes(b"\0" + b"a" * 7)
            (repo / "b.md").write_text("12345", encoding="utf-8")
            original_read_bytes = Path.read_bytes
            read_paths: list[str] = []

            def read_bytes(path: Path) -> bytes:
                read_paths.append(path.name)
                if path.name == "b.md":
                    raise AssertionError("b.md should be skipped before read")
                return original_read_bytes(path)

            with patch.object(Path, "read_bytes", read_bytes):
                bundle = collect_generated_discovery_evidence(
                    repo,
                    limits=EvidenceLimits(max_file_bytes=10, max_total_bytes=10),
                )

        self.assertEqual(bundle.files, ())
        self.assertEqual(read_paths, ["a-binary.md"])
        skipped = {(item.path, item.reason) for item in bundle.skipped}
        self.assertIn(("a-binary.md", "binary_file"), skipped)
        self.assertIn(("b.md", "total_size_limit"), skipped)

    def test_redacts_secret_values_before_prompt_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "PLAN.md").write_text(
                "password = supersecret\n"
                "token: abc.def.ghi\n"
                'secret: "value, {with braces}"\n'
                '"api_key" = "quoted-key-secret"\n'
                "'token' = 'single-quoted-key-secret'\n"
                '"token": yaml-quoted-key-secret\n'
                "'password': yaml-single-quoted-key-secret\n"
                'credentials = "credential-secret"\n'
                "credential: credential-secret\n"
                "AWS_CREDENTIALS=credential-secret\n"
                "AWS_ACCESS_KEY_ID=aws-access-id-secret\n"
                "GITHUB_PAT_TOKEN=pat-key-secret\n"
                "ghp_token: ghp-key-secret\n"
                "api-key-rotation: arbitrary-action-secret\n"
                '"auth-migration": "arbitrary-json-action-secret"\n'
                "api-key-rotation: |\n"
                "  arbitrary-multiline-action-secret\n"
                "Password Reset: pass-reset-secret\n"
                "Auth Migration: auth-migration-direct-secret\n"
                "Auth Rotation: auth-rotation-direct-secret\n"
                "Auth Reset: auth-reset-direct-secret\n"
                "Password Rotation: password-rotation-direct-secret\n"
                "Password Migration: password-migration-direct-secret\n"
                "Password Cleanup: |\n"
                "  password-cleanup-block-secret\n"
                "Token Cleanup: token-cleanup-direct-secret\n"
                "API_KEY_ROTATION=api-rotation-secret\n"
                "client-secret-rotation: client-rotation-secret\n"
                "DB_PASS=db-pass-secret\n"
                "MYSQL_PASS: mysql-pass-secret\n"
                "SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T000/B000/secret\n"
                "DISCORD_WEBHOOK=https://discord.com/api/webhooks/1/secret\n"
                "SLACK_WEBHOOK=opaque-slack-webhook-secret\n"
                "DISCORD_WEBHOOK: opaque-discord-webhook-secret\n"
                "ESCAPED_SLACK_WEBHOOK_URL=https:\\/\\/hooks.slack.com\\/services\\/T777\\/B888\\/escapedsecret\n"
                "ESCAPED_DISCORD_WEBHOOK=https:\\/\\/discord.com\\/api\\/webhooks\\/3\\/escapedsecret\n"
                "PERCENT_SLACK_WEBHOOK_URL=https%3A%2F%2Fhooks%2Eslack%2Ecom%2Fservices%2FT000%2FB000%2Fpercentsecret\n"
                "MIXED_SLACK_WEBHOOK_URL=https://hooks%2Eslack%2Ecom%2Fservices%2FT000%2FB000%2Fmixedsecret\n"
                "DOUBLE_SLACK_WEBHOOK_URL=https%253A%252F%252Fhooks%252Eslack%252Ecom%252Fservices%252FT000%252FB000%252Fdoublesecret\n"
                "TRIPLE_SLACK_WEBHOOK_URL=https%25253A%25252F%25252Fhooks%25252Eslack%25252Ecom%25252Fservices%25252FT000%25252FB000%25252Ftriplesecret\n"
                "QUERY_SLACK_WEBHOOK_URL=https%3A%2F%2Fhooks%2Eslack%2Ecom%2Fservices%2FT000%2FB000%2Fpathsecret%3Ftoken%3Dquerysecret\n"
                "ESCAPED_PERCENT_SLACK_WEBHOOK_URL=https:\\/\\/hooks%2Eslack%2Ecom\\/services\\/T000\\/B000\\/escapedpercentsecret\n"
                "NO_SCHEME_SLACK_WEBHOOK=hooks.slack.com/services/T000/B000/noschemesecret\n"
                "ENCODED_NO_SCHEME_SLACK_WEBHOOK=hooks%2Eslack%2Ecom%2Fservices%2FT000%2FB000%2Fencodednoschemesecret\n"
                "FLATTENED_SLACK_WEBHOOK=hooks.slack.com-services-T000-B000-flattenedsecret\n"
                "ENCODED_FLATTENED_SLACK_WEBHOOK=hooks%2Eslack%2Ecom-services-T000-B000-encodedflattenedsecret\n"
                "MIXED_NO_SCHEME_SLACK_WEBHOOK=hooks.slack.com%2Fservices%2FT000%2FB000%2Fmixednoschemesecret\n"
                "ENCODED_HOST_NO_SCHEME_SLACK_WEBHOOK=hooks%2Eslack%2Ecom/services/T000/B000/encodedhostnoschemesecret\n"
                "ESCAPED_NO_SCHEME_SLACK_WEBHOOK=hooks.slack.com\\/services\\/T000\\/B000\\/escapednoschemesecret\n"
                r"DOUBLE_ESCAPED_SLACK_WEBHOOK_URL=https:\\/\\/hooks.slack.com\\/services\\/T000\\/B000\\/doubleescapedsecret"
                "\n"
                r"ESCAPED_DOT_SLACK_WEBHOOK_URL=https:\/\/hooks\\.slack\\.com\/services\/T000\/B000\/escapeddotsecret"
                "\n"
                "UNICODE_SLACK_WEBHOOK_URL=https:\\u003a\\u002f\\u002fhooks.slack.com\\u002fservices\\u002fT000\\u002fB000\\u002funicodesecret\n"
                "DOUBLE_UNICODE_SLACK_WEBHOOK_URL=https:\\\\u003a\\\\u002f\\\\u002fhooks.slack.com\\\\u002fservices\\\\u002fT000\\\\u002fB000\\\\u002fdoubleunicodesecret\n"
                "FULLY_PERCENT_SLACK_WEBHOOK_URL=%68%74%74%70%73%3A%2F%2Fhooks%2Eslack%2Ecom%2Fservices%2FT000%2FB000%2Ffullypercentsecret\n"
                "PERCENT_UNICODE_SLACK_WEBHOOK_URL=%5Cu0068%5Cu006f%5Cu006f%5Cu006b%5Cu0073%5Cu002e%5Cu0073%5Cu006c%5Cu0061%5Cu0063%5Cu006b%5Cu002e%5Cu0063%5Cu006f%5Cu006d%5Cu002f%5Cu0073%5Cu0065%5Cu0072%5Cu0076%5Cu0069%5Cu0063%5Cu0065%5Cu0073%5Cu002f%5Cu0054%5Cu0030%5Cu0030%5Cu0030%5Cu002f%5Cu0042%5Cu0030%5Cu0030%5Cu0030%5Cu002f%5Cu0070%5Cu0065%5Cu0072%5Cu0063%5Cu0065%5Cu006e%5Cu0074%5Cu0075%5Cu006e%5Cu0069%5Cu0063%5Cu006f%5Cu0064%5Cu0065%5Cu0073%5Cu0065%5Cu0063%5Cu0072%5Cu0065%5Cu0074\n"
                "PARTIAL_PERCENT_SLACK_WEBHOOK_URL=%68ooks.slack.com/services/T000/B000/partialpercentsecret\n"
                "INTERIOR_PERCENT_SLACK_WEBHOOK_URL=h%6Foks.slack.com/services/T000/B000/interiorpercentsecret\n"
                "INTERIOR_PERCENT_SLACK_WEBHOOK_QUERY=h%6Foks.slack.com/services/T000/B000/querytokensecret?foo=querysecret#fragsecret\n"
                "INTERIOR_PERCENT_SLACK_WEBHOOK_PUNCT=h%6Foks.slack.com/services/T000/B000/pathsecret?foo=querysecret;bar=semicolonsecret,comma=commasecret#fragsecret\n"
                r"RAW_UNICODE_SLACK_WEBHOOK_URL=\u0068ooks.slack.com/services/T000/B000/rawunicodesecret"
                "\n"
                "PARTIAL_PERCENT_UNICODE_DISCORD_WEBHOOK_URL=%5Cu0064iscord.com/api/webhooks/1/partialunicodediscordsecret\n"
                "INTERIOR_PERCENT_DISCORD_WEBHOOK_URL=di%73cord.com/api/webhooks/1/interiordiscordsecret\n"
                r"RAW_UNICODE_DISCORD_WEBHOOK_URL=\u0064iscord.com/api/webhooks/1/rawunicodediscordsecret"
                "\n"
                "NO_SCHEME_DISCORD_WEBHOOK=discord.com/api/webhooks/123/discordnoschemesecret\n"
                "FLATTENED_DISCORD_WEBHOOK=discord.com-api-webhooks-123-discordflattenedsecret\n"
                "FULLY_PERCENT_DISCORD_WEBHOOK_URL=%68%74%74%70%73%3A%2F%2Fdiscord.com%2Fapi%2Fwebhooks%2F1%2Ffullydiscordsecret\n"
                "PERCENT_DISCORD_WEBHOOK_URL=https%3A%2F%2Fdiscord.com%2Fapi%2Fwebhooks%2F1%2Fdiscordsecret\n"
                "ESCAPED_TOKEN_USERINFO_URL=https:\\/\\/tokenabc@example.com\\/repo.git\n"
                r"DOUBLE_ESCAPED_TOKEN_USERINFO_URL=https:\\/\\/doubletokenabc@example.com\\/repo.git"
                "\n"
                r"DOUBLE_ESCAPED_BASIC_URL=https:\\/\\/user:doublepasssecret@example.com\\/repo.git"
                "\n"
                "ENCODED_TOKEN_USERINFO_URL=https://github%5Fpat%5Fabc@example.com/repo.git\n"
                "ENCODED_BASIC_TOKEN_USERINFO_URL=https://github%5Fpat%5Fabc:x-oauth-basic@example.com/repo.git\n"
                "Bare Slack hook: https://hooks.slack.com/services/T111/B222/baresecret\n"
                "Bare Discord hook: https://discord.com/api/webhooks/2/baresecret\n"
                "Webhook URL: https://hooks.slack.com/services/T333/B444/spacedsecret\n"
                "Slack Webhook URL: https://hooks.slack.com/services/T555/B666/slackspaced\n"
                "access_key_id: access-key-id-secret\n"
                "service_account_key = service-account-secret\n"
                "service-account-key: service-account-secret\n"
                "AWS Access Key ID: spaced-access-key-id-secret\n"
                "Service Account Key: spaced-service-account-secret\n"
                "API key: spaced-api-secret\n"
                "API key: |\n"
                "  spaced multiline api secret\n"
                "OpenAI API key: prefixed-api-secret\n"
                "- API key: list-api-secret\n"
                '"api key": "quoted-api-secret"\n'
                "api.key = dotted-api-secret\n"
                "Client Secret: |\n"
                "  spaced multiline client secret\n"
                "AWS Credentials: |\n"
                "  spaced multiline credentials secret\n"
                "Bearer Token: bearer-token-secret\n"
                "Database Password: |\n"
                "  database password body\n"
                "Access Token: |\n"
                "  access token body\n"
                "- name: DATABASE_PASSWORD\n"
                "  value: env-secret-value\n"
                '- name: "DATABASE_PASSWORD"\n'
                "  value : quoted-name-secret\n"
                "- name: DATABASE_PASSWORD # comment\n"
                "  value: commented-name-secret\n"
                '- { name: ACCESS_TOKEN, value: "flow-secret" }\n'
                '- { value: "reverse-flow-secret", name: DATABASE_PASSWORD }\n'
                '- { "name": "ACCESS_TOKEN", "value": "quoted-flow-secret" }\n'
                'env: [{ value: "env-array-secret", name: DATABASE_PASSWORD }]\n'
                "env: [ {\n"
                '  value: "wrapped-env-array-secret"\n'
                "  name: DATABASE_PASSWORD\n"
                "}]\n"
                "env: [\n"
                "  {\n"
                "    value: env-split-secret\n"
                "    name: DATABASE_PASSWORD\n"
                "  }\n"
                "]\n"
                "environment:\n"
                "  - name: SERVICE_ACCOUNT_KEY\n"
                "    value: service-list-secret\n"
                "  - name: AUTHORIZATION\n"
                "    value: authz-list-secret\n"
                "- {\n"
                '  value: "wrapped-flow-secret"\n'
                "  name: DATABASE_PASSWORD\n"
                "}\n"
                '- { "value": "double-secret",\n'
                '  "name": "DATABASE_PASSWORD" }\n'
                "- {\n"
                "  name: DATABASE_PASSWORD\n"
                '  value: "wrapped-forward-secret"\n'
                "}\n"
                "- value: |\n"
                "  value-first block secret\n"
                "  name: DATABASE_PASSWORD\n"
                "- value: &pw |\n"
                "  tagged value-first block secret\n"
                "  name: DATABASE_PASSWORD\n"
                "name: DATABASE_PASSWORD\n"
                "value: split-object-secret\n"
                "name: DATABASE_PASSWORD\n"
                "# comment between name and value\n"
                "value: comment-gap-db-secret\n"
                '"name": "DATABASE_PASSWORD"\n'
                '"value": "quoted-split-secret"\n'
                '"name": "DATABASE_PASSWORD",\n'
                '"value": "json-comma-split-secret"\n'
                'name = "DATABASE_PASSWORD"\n'
                'value = "toml-split-secret"\n'
                '"value": "json-reverse-secret",\n'
                '"name": "DATABASE_PASSWORD"\n'
                'value = "toml-reverse-secret"\n'
                'name = "DATABASE_PASSWORD"\n'
                'env = [{ name = "DATABASE_PASSWORD", value = "toml-inline-secret" }]\n'
                "environment: [{ name: DATABASE_PASSWORD,\n"
                '  value: "environment-flow-secret" }]\n'
                'environment: [{ value: "environment-value-first-secret",\n'
                "  name: DATABASE_PASSWORD }]\n"
                "variables: [{ key: ACCESS_TOKEN,\n"
                '  value: "variables-flow-secret" }]\n'
                'variables: [{ name: AUTH-01, key: ACCESS_TOKEN, value: "mixed-key-secret" }]\n'
                'environment: { value: "keyed-environment-secret",\n'
                "  name: DATABASE_PASSWORD }\n"
                "variables: [\n"
                '  { value: "split-array-secret",\n'
                "    key: ACCESS_TOKEN }\n"
                "]\n"
                "- name: !!str API Key\n"
                "  value: tagged-api-key-secret\n"
                "- name: &n API Key\n"
                "  value: anchored-api-key-secret\n"
                "- name: DB_PASS\n"
                "  value: db-pass-list-secret\n"
                "name: access_key_id\n"
                "value: access-id-field-secret\n"
                "name: AWS Access Key ID\n"
                "value: aws-access-key-label-secret\n"
                "name: github_pat\n"
                "value: github-pat-field-secret\n"
                "name: GitHub PAT\n"
                "value: github-pat-spaced-secret\n"
                "name: client_key_data\n"
                "value: client-key-data-secret\n"
                "name: apikey\n"
                "value: compact-apikey-secret\n"
                "name: clientsecret\n"
                "value: compact-clientsecret-secret\n"
                "name: accesskeyid\n"
                "value: compact-accesskeyid-secret\n"
                "name: serviceaccountkey\n"
                "value: compact-serviceaccountkey-secret\n"
                "name: credentials\n"
                "value: compact-credentials-secret\n"
                "name: oauth\n"
                "value: compact-oauth-secret\n"
                "key: api-key\n"
                "value: api-key-field-secret\n"
                "name: client-secret\n"
                "value: client-secret-field-secret\n"
                "name: database-password\n"
                "value: database-password-field-secret\n"
                'name: "API Key"\n'
                "value: api-key-display-secret\n"
                'name = "Client Secret"\n'
                "value = client-secret-display-secret\n"
                '- { name: "Access Token", value: "access-token-flow-secret" }\n'
                "name: API Key\n"
                "value: api-key-unquoted-secret\n"
                "- name: Client Secret\n"
                "  value: client-secret-list-unquoted\n"
                '- { name: Access Token, value: "access-token-unquoted-flow-secret" }\n'
                '- { value: "access-token-unquoted-value-first-secret", name: Access Token }\n'
                "key: ACCESS_TOKEN\n"
                "value: key-split-secret\n"
                "- name: AUTH-01\n"
                "  value: same-item-forward-key-secret\n"
                "  key: ACCESS_TOKEN\n"
                "value: root-delayed-secret\n"
                "description: between value and name\n"
                "name: DATABASE_PASSWORD\n"
                "- value: delayed-value-secret\n"
                "  description: between value and name\n"
                "  name: DATABASE_PASSWORD\n"
                "tasks:\n"
                "  - {\n"
                "      id: DISC-09\n"
                "    }\n"
                "- name: ACCESS_TOKEN\n"
                "  value: |\n"
                "    env block secret\n"
                "secret key: spaced-secret-key\n"
                "AWS Secret Access Key: aws-secret-access\n"
                "private key: spaced-private-key\n"
                "private.key: dotted-private-key\n"
                "- password: list-secret\n"
                '- "password": quoted-list-secret\n'
                "token: |\n"
                "  multiline secret one\n"
                "  multiline secret two\n"
                "- { id: DISC-10 }\n"
                "password: |2\n"
                "    indented indicator secret\n"
                "password: !vault |\n"
                "  tagged password block secret\n"
                "password: &pw |\n"
                "  anchored password block secret\n"
                "api_key: !!str |\n"
                "  tagged api key block secret\n"
                "secret:\n"
                "  user: alice\n"
                "  value: nested-secret\n"
                "tokens:\n"
                "- same-indent-secret\n"
                "public: after-yaml-block\n"
                'password = """first triple secret\n'
                "toml multiline secret\n"
                '"""\n'
                "secret = [\n"
                '"""\n'
                "not a closing ] marker\n"
                '""",\n'
                '"after-bracket-secret"\n'
                "]\n"
                "API_KEY=bare-secret\n"
                "TOKEN='quoted-secret'\n"
                "Authorization: Bearer raw-token-value\n"
                "bare tokens: github_pat_baretoken ghp_baretoken glpat-baretoken xoxb-baretoken\n"
                "author: Alice\n"
                "SLACK_WEBHOOK_INTEGRATION=visible-slack-webhook-docs\n"
                "slack-webhook-integration: visible dashed slack docs\n"
                "DISCORD_WEBHOOK_DOCS=visible-discord-docs\n"
                "Webhook Integration: implement docs\n"
                "Slack Webhook Integration: implement docs\n"
                "Discord webhook docs: visible\n"
                "authentication: task docs\n"
                "tokenization: task docs\n"
                "webhook: normal docs\n"
                "normal: visible\n",
                encoding="utf-8",
            )
            raw_plan_hash = hashlib.sha256((repo / "PLAN.md").read_bytes()).hexdigest()

            bundle = collect_generated_discovery_evidence(repo)

        prompt = bundle.prompt_input_json()
        content = prompt["files"][0]["content"]
        self.assertNotIn("supersecret", content)
        self.assertNotIn("abc.def.ghi", content)
        self.assertNotIn("value, {with braces}", content)
        self.assertNotIn("quoted-key-secret", content)
        self.assertNotIn("single-quoted-key-secret", content)
        self.assertNotIn("yaml-quoted-key-secret", content)
        self.assertNotIn("yaml-single-quoted-key-secret", content)
        self.assertNotIn("credential-secret", content)
        self.assertNotIn("aws-access-id-secret", content)
        self.assertNotIn("pat-key-secret", content)
        self.assertNotIn("ghp-key-secret", content)
        self.assertNotIn("arbitrary-action-secret", content)
        self.assertNotIn("arbitrary-json-action-secret", content)
        self.assertNotIn("arbitrary-multiline-action-secret", content)
        self.assertNotIn("pass-reset-secret", content)
        self.assertNotIn("auth-migration-direct-secret", content)
        self.assertNotIn("auth-rotation-direct-secret", content)
        self.assertNotIn("auth-reset-direct-secret", content)
        self.assertNotIn("password-rotation-direct-secret", content)
        self.assertNotIn("password-migration-direct-secret", content)
        self.assertNotIn("password-cleanup-block-secret", content)
        self.assertNotIn("token-cleanup-direct-secret", content)
        self.assertNotIn("api-rotation-secret", content)
        self.assertNotIn("client-rotation-secret", content)
        self.assertNotIn("db-pass-secret", content)
        self.assertNotIn("mysql-pass-secret", content)
        self.assertNotIn("hooks.slack.com/services/T000/B000/secret", content)
        self.assertNotIn("discord.com/api/webhooks/1/secret", content)
        self.assertNotIn("opaque-slack-webhook-secret", content)
        self.assertNotIn("opaque-discord-webhook-secret", content)
        self.assertNotIn(
            "hooks.slack.com\\/services\\/T777\\/B888\\/escapedsecret", content
        )
        self.assertNotIn("discord.com\\/api\\/webhooks\\/3\\/escapedsecret", content)
        self.assertNotIn("percentsecret", content)
        self.assertNotIn("mixedsecret", content)
        self.assertNotIn("doublesecret", content)
        self.assertNotIn("triplesecret", content)
        self.assertNotIn("pathsecret", content)
        self.assertNotIn("querysecret", content)
        self.assertNotIn("escapedpercentsecret", content)
        self.assertNotIn("noschemesecret", content)
        self.assertNotIn("encodednoschemesecret", content)
        self.assertNotIn("flattenedsecret", content)
        self.assertNotIn("encodedflattenedsecret", content)
        self.assertNotIn("mixednoschemesecret", content)
        self.assertNotIn("encodedhostnoschemesecret", content)
        self.assertNotIn("escapednoschemesecret", content)
        self.assertNotIn("doubleescapedsecret", content)
        self.assertNotIn("escapeddotsecret", content)
        self.assertNotIn("unicodesecret", content)
        self.assertNotIn("doubleunicodesecret", content)
        self.assertNotIn("fullypercentsecret", content)
        self.assertNotIn("percentunicodesecret", content)
        self.assertNotIn("partialpercentsecret", content)
        self.assertNotIn("interiorpercentsecret", content)
        self.assertNotIn("querytokensecret", content)
        self.assertNotIn("querysecret", content)
        self.assertNotIn("fragsecret", content)
        self.assertNotIn("pathsecret", content)
        self.assertNotIn("semicolonsecret", content)
        self.assertNotIn("commasecret", content)
        self.assertNotIn("rawunicodesecret", content)
        self.assertNotIn("partialunicodediscordsecret", content)
        self.assertNotIn("interiordiscordsecret", content)
        self.assertNotIn("rawunicodediscordsecret", content)
        self.assertNotIn("discordnoschemesecret", content)
        self.assertNotIn("discordflattenedsecret", content)
        self.assertNotIn("fullydiscordsecret", content)
        self.assertNotIn("discordsecret", content)
        self.assertNotIn("tokenabc", content)
        self.assertNotIn("doubletokenabc", content)
        self.assertNotIn("doublepasssecret", content)
        self.assertNotIn("github%5Fpat%5Fabc", content)
        self.assertNotIn("x-oauth-basic", content)
        self.assertNotIn("hooks.slack.com/services/T111/B222/baresecret", content)
        self.assertNotIn("discord.com/api/webhooks/2/baresecret", content)
        self.assertNotIn("hooks.slack.com/services/T333/B444/spacedsecret", content)
        self.assertNotIn("hooks.slack.com/services/T555/B666/slackspaced", content)
        self.assertNotIn("access-key-id-secret", content)
        self.assertNotIn("service-account-secret", content)
        self.assertNotIn("spaced-access-key-id-secret", content)
        self.assertNotIn("spaced-service-account-secret", content)
        self.assertNotIn("spaced-api-secret", content)
        self.assertNotIn("spaced multiline api secret", content)
        self.assertNotIn("prefixed-api-secret", content)
        self.assertNotIn("list-api-secret", content)
        self.assertNotIn("quoted-api-secret", content)
        self.assertNotIn("dotted-api-secret", content)
        self.assertNotIn("spaced multiline client secret", content)
        self.assertNotIn("spaced multiline credentials secret", content)
        self.assertNotIn("bearer-token-secret", content)
        self.assertNotIn("database password body", content)
        self.assertNotIn("access token body", content)
        self.assertNotIn("env-secret-value", content)
        self.assertNotIn("quoted-name-secret", content)
        self.assertNotIn("commented-name-secret", content)
        self.assertNotIn("flow-secret", content)
        self.assertNotIn("reverse-flow-secret", content)
        self.assertNotIn("quoted-flow-secret", content)
        self.assertNotIn("env-array-secret", content)
        self.assertNotIn("wrapped-env-array-secret", content)
        self.assertNotIn("env-split-secret", content)
        self.assertNotIn("service-list-secret", content)
        self.assertNotIn("authz-list-secret", content)
        self.assertNotIn("wrapped-flow-secret", content)
        self.assertNotIn("double-secret", content)
        self.assertNotIn("wrapped-forward-secret", content)
        self.assertNotIn("value-first block secret", content)
        self.assertNotIn("tagged value-first block secret", content)
        self.assertNotIn("split-object-secret", content)
        self.assertNotIn("comment-gap-db-secret", content)
        self.assertNotIn("quoted-split-secret", content)
        self.assertNotIn("json-comma-split-secret", content)
        self.assertNotIn("toml-split-secret", content)
        self.assertNotIn("json-reverse-secret", content)
        self.assertNotIn("toml-reverse-secret", content)
        self.assertNotIn("toml-inline-secret", content)
        self.assertNotIn("environment-flow-secret", content)
        self.assertNotIn("environment-value-first-secret", content)
        self.assertNotIn("variables-flow-secret", content)
        self.assertNotIn("mixed-key-secret", content)
        self.assertNotIn("keyed-environment-secret", content)
        self.assertNotIn("split-array-secret", content)
        self.assertNotIn("tagged-api-key-secret", content)
        self.assertNotIn("anchored-api-key-secret", content)
        self.assertNotIn("db-pass-list-secret", content)
        self.assertNotIn("access-id-field-secret", content)
        self.assertNotIn("aws-access-key-label-secret", content)
        self.assertNotIn("github-pat-field-secret", content)
        self.assertNotIn("github-pat-spaced-secret", content)
        self.assertNotIn("client-key-data-secret", content)
        self.assertNotIn("compact-apikey-secret", content)
        self.assertNotIn("compact-clientsecret-secret", content)
        self.assertNotIn("compact-accesskeyid-secret", content)
        self.assertNotIn("compact-serviceaccountkey-secret", content)
        self.assertNotIn("compact-credentials-secret", content)
        self.assertNotIn("compact-oauth-secret", content)
        self.assertNotIn("api-key-field-secret", content)
        self.assertNotIn("client-secret-field-secret", content)
        self.assertNotIn("database-password-field-secret", content)
        self.assertNotIn("api-key-display-secret", content)
        self.assertNotIn("client-secret-display-secret", content)
        self.assertNotIn("access-token-flow-secret", content)
        self.assertNotIn("api-key-unquoted-secret", content)
        self.assertNotIn("client-secret-list-unquoted", content)
        self.assertNotIn("access-token-unquoted-flow-secret", content)
        self.assertNotIn("access-token-unquoted-value-first-secret", content)
        self.assertNotIn("key-split-secret", content)
        self.assertNotIn("same-item-forward-key-secret", content)
        self.assertNotIn("root-delayed-secret", content)
        self.assertNotIn("delayed-value-secret", content)
        self.assertIn("id: DISC-09", content)
        self.assertNotIn("env block secret", content)
        self.assertNotIn("spaced-secret-key", content)
        self.assertNotIn("aws-secret-access", content)
        self.assertNotIn("spaced-private-key", content)
        self.assertNotIn("dotted-private-key", content)
        self.assertNotIn("list-secret", content)
        self.assertNotIn("quoted-list-secret", content)
        self.assertNotIn("multiline secret one", content)
        self.assertNotIn("multiline secret two", content)
        self.assertIn("id: DISC-10", content)
        self.assertNotIn("indented indicator secret", content)
        self.assertNotIn("tagged password block secret", content)
        self.assertNotIn("anchored password block secret", content)
        self.assertNotIn("tagged api key block secret", content)
        self.assertNotIn("nested-secret", content)
        self.assertNotIn("same-indent-secret", content)
        self.assertNotIn("first triple secret", content)
        self.assertNotIn("toml multiline secret", content)
        self.assertNotIn("after-bracket-secret", content)
        self.assertNotIn("bare-secret", content)
        self.assertNotIn("quoted-secret", content)
        self.assertNotIn("raw-token-value", content)
        self.assertNotIn("github_pat_baretoken", content)
        self.assertNotIn("ghp_baretoken", content)
        self.assertNotIn("glpat-baretoken", content)
        self.assertNotIn("xoxb-baretoken", content)
        self.assertIn("author: Alice", content)
        self.assertIn("SLACK_WEBHOOK_INTEGRATION=visible-slack-webhook-docs", content)
        self.assertIn("slack-webhook-integration: visible dashed slack docs", content)
        self.assertIn("DISCORD_WEBHOOK_DOCS=visible-discord-docs", content)
        self.assertIn("Webhook Integration: implement docs", content)
        self.assertIn("Slack Webhook Integration: implement docs", content)
        self.assertIn("Discord webhook docs: visible", content)
        self.assertIn("authentication: task docs", content)
        self.assertIn("tokenization: task docs", content)
        self.assertIn("webhook: normal docs", content)
        self.assertIn("public: after-yaml-block", content)
        self.assertIn("normal: visible", content)
        self.assertTrue(prompt["files"][0]["redacted"])
        self.assertNotEqual(prompt["files"][0]["sha256"], raw_plan_hash)
        self.assertEqual(
            prompt["files"][0]["sha256"],
            hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )

    def test_keeps_bracketed_task_sections_while_redacting_secret_sections(
        self,
    ) -> None:
        text = (
            "[AUTH-01]\n"
            "Scope: keep authentication task evidence\n"
            "[Password Reset]\n"
            "Scope: keep password reset task evidence\n"
            "[Auth Migration]\n"
            "Scope: keep auth migration task evidence\n"
            "[secrets]\n"
            'foo = "section-secret"\n'
            "[API Key]\n"
            'value = "api-key-section-secret"\n'
            "[OpenAI API Key]\n"
            'value = "openai-api-key-section-secret"\n'
            "[GitHub PAT]\n"
            'value = "github-pat-section-secret"\n'
            "[Service Account Key]\n"
            'value = "service-account-section-secret"\n'
            "[Access Key ID]\n"
            'value = "access-key-id-section-secret"\n'
            "[secrets.password-reset]\n"
            'value = "reset-section-secret"\n'
            "[client-key-data]\n"
            'value = "client-key-section-secret"\n'
            "[webhook]\n"
            "value = normal webhook docs\n"
            "[AUTH-02]\n"
            "Acceptance: keep follow-up task evidence\n"
        )

        redacted = redact_evidence_text(text)

        self.assertIn("[AUTH-01]", redacted)
        self.assertIn("Scope: keep authentication task evidence", redacted)
        self.assertIn("[Password Reset]", redacted)
        self.assertIn("Scope: keep password reset task evidence", redacted)
        self.assertIn("[Auth Migration]", redacted)
        self.assertIn("Scope: keep auth migration task evidence", redacted)
        self.assertNotIn("section-secret", redacted)
        self.assertNotIn("api-key-section-secret", redacted)
        self.assertNotIn("openai-api-key-section-secret", redacted)
        self.assertNotIn("github-pat-section-secret", redacted)
        self.assertNotIn("service-account-section-secret", redacted)
        self.assertNotIn("access-key-id-section-secret", redacted)
        self.assertNotIn("reset-section-secret", redacted)
        self.assertNotIn("client-key-section-secret", redacted)
        self.assertIn("[webhook]", redacted)
        self.assertIn("value = normal webhook docs", redacted)
        self.assertIn("[AUTH-02]", redacted)
        self.assertIn("Acceptance: keep follow-up task evidence", redacted)

    def test_redacts_common_secret_forms_in_allowed_config_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".vibe-loop.toml").write_text(
                "[agent]\n"
                'command = "OPENAI_API_KEY=sk-secret codex exec --api-key raw-key"\n'
                'selection_command = "GITHUB_TOKEN=ghp-secret codex exec"\n',
                encoding="utf-8",
            )
            (repo / "config.toml").write_text(
                "command = \"API_KEY='quoted-shell-secret' cmd "
                '--api-key "quoted-flag-secret" --auth registry-secret '
                '--authorization bearer-secret"\n'
                'adjacent = "--authorization bearer --password pass-secret"\n'
                'missing = "--api-key --password chained-secret"\n'
                "spaced = \"TOKEN='secret part' cmd --password 'word gap' "
                '--api-key="key gap""\n'
                'escaped = "TOKEN=\\"escaped secret\\" cmd '
                '--api-key=\\"escaped key\\""\n'
                'bearerflag = "--authorization Bearer bearer-flag-secret '
                '--access-token access-flag-secret --oauth-token oauth-flag-secret"\n'
                'bearerequals = "--authorization=Bearer equals-bearer-secret '
                '--auth=Bearer auth-equals-secret"\n'
                'quotedbearer = "--authorization Bearer \\"quoted-bearer-secret\\" '
                "--access-token Bearer 'quoted-access-secret' "
                '--auth=Bearer \\"quoted-auth-secret\\""\n'
                "GITHUB_PAT=plainsecret\n"
                "literal_openai = sk-proj-1234567890abcdef\n"
                "literal_aws = AKIAIOSFODNN7EXAMPLE\n"
                "literal_slack = xoxp-1234567890-1234567890-secret\n"
                "literal_slack_cookie = xoxc-secret-cookie\n"
                "slack_url = https://xoxc-secret-cookie@example.com/repo.git\n"
                "[secrets]\n"
                'foo = "toml-section-secret"\n'
                "bar = toml-section-bare-secret\n"
                "[project]\n"
                'name = "visible-project"\n'
                "[auth]\n"
                'endpoint = "auth-section-secret"\n'
                "[safe]\n"
                'value = "public-section-value"\n',
                encoding="utf-8",
            )
            (repo / "xoxc-secret-cookie.md").write_text("secret\n", encoding="utf-8")

            bundle = collect_generated_discovery_evidence(repo)

        contents = {
            file["path"]: file["content"]
            for file in bundle.prompt_input_json()["files"]
        }
        content = "\n".join(contents.values())
        self.assertNotIn("sk-secret", content)
        self.assertNotIn("raw-key", content)
        self.assertNotIn("ghp-secret", content)
        self.assertNotIn("quoted-shell-secret", content)
        self.assertNotIn("quoted-flag-secret", content)
        self.assertNotIn("registry-secret", content)
        self.assertNotIn("bearer-secret", content)
        self.assertNotIn("pass-secret", content)
        self.assertNotIn("chained-secret", content)
        self.assertNotIn("secret part", content)
        self.assertNotIn("word gap", content)
        self.assertNotIn("key gap", content)
        self.assertNotIn("escaped secret", content)
        self.assertNotIn("escaped key", content)
        self.assertNotIn("bearer-flag-secret", content)
        self.assertNotIn("access-flag-secret", content)
        self.assertNotIn("oauth-flag-secret", content)
        self.assertNotIn("equals-bearer-secret", content)
        self.assertNotIn("auth-equals-secret", content)
        self.assertNotIn("quoted-bearer-secret", content)
        self.assertNotIn("quoted-access-secret", content)
        self.assertNotIn("quoted-auth-secret", content)
        self.assertNotIn("plainsecret", content)
        self.assertNotIn("sk-proj-1234567890abcdef", content)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", content)
        self.assertNotIn("xoxp-1234567890-1234567890-secret", content)
        self.assertNotIn("xoxc-secret-cookie", content)
        self.assertNotIn("toml-section-secret", content)
        self.assertNotIn("toml-section-bare-secret", content)
        self.assertNotIn("auth-section-secret", content)
        self.assertIn("OPENAI_API_KEY=<redacted>", content)
        self.assertIn("--api-key <redacted>", content)
        self.assertIn("--auth <redacted>", content)
        self.assertIn("--authorization <redacted>", content)
        self.assertIn("--authorization Bearer <redacted>", content)
        self.assertIn("--access-token <redacted>", content)
        self.assertIn("--oauth-token <redacted>", content)
        self.assertIn("--password <redacted>", content)
        self.assertIn("GITHUB_TOKEN=<redacted>", content)
        self.assertIn("GITHUB_PAT=<redacted>", content)
        self.assertIn("API_KEY='<redacted>'", contents["config.toml"])
        self.assertIn('--api-key "<redacted>"', contents["config.toml"])
        self.assertIn("TOKEN='<redacted>'", contents["config.toml"])
        self.assertIn("--password '<redacted>'", contents["config.toml"])
        self.assertIn('--api-key="<redacted>"', contents["config.toml"])
        self.assertIn('name = "visible-project"', contents["config.toml"])
        self.assertIn('value = "public-section-value"', contents["config.toml"])
        self.assertIn(
            ("<redacted>.md", "secret_path"),
            {(item.path, item.reason) for item in bundle.skipped},
        )

    def test_preserves_non_secret_value_first_task_fields(self) -> None:
        text = (
            '- { value: "visible inline value", id: DISC-09 }\n'
            "- value: visible list value\n"
            "  id: DISC-10\n"
            "value: visible top-level value\n"
            "name: DISPLAY_NAME\n"
            "filename: AUTH.md\n"
            "username: AUTH_USER\n"
            "name: AUTH-01\n"
            "value: visible auth task value\n"
            "display name: TOKENIZATION\n"
            "task name: PASSWORD RESET\n"
            "name: Password Reset\n"
            "name: Auth Migration\n"
            "name: API Key Rotation\n"
            '"api-key-rotation": "visible quoted rotation"\n'
            "notes: document secret scanning behavior\n"
            "- name: API Key Rotation\n"
            "  value: rotate visible docs\n"
            "name: API_KEY_ROTATION\n"
            "value: api-rotation-split-secret\n"
            "name: .api-key-rotation\n"
            "value: hidden-api-rotation-split-secret\n"
            "name: api-key-rotation.\n"
            "value: punct-api-rotation-split-secret\n"
            "name: API_KEY Rotation\n"
            "value: mixed-natural-title-secret\n"
            "name: .API Key Rotation\n"
            "value: punct-natural-title-secret\n"
            "name: API-Key-Rotation\n"
            "value: cased-api-rotation-secret\n"
            "name: Auth-Migration\n"
            "value: cased-auth-migration-secret\n"
            "name: Password-Reset\n"
            "value: cased-password-reset-secret\n"
            "name: Token-Cleanup\n"
            "value: cased-token-cleanup-secret\n"
            "value: visible before title\n"
            "name: Password Reset\n"
            "name: AUTH-01\n"
            "value: visible auth task value after title\n"
            "compass: visible direction\n"
        )

        redacted = redact_evidence_text(text)

        self.assertIn("visible inline value", redacted)
        self.assertIn("visible list value", redacted)
        self.assertIn("visible top-level value", redacted)
        self.assertIn("name: DISPLAY_NAME", redacted)
        self.assertIn("filename: AUTH.md", redacted)
        self.assertIn("username: AUTH_USER", redacted)
        self.assertIn("name: AUTH-01", redacted)
        self.assertIn("visible auth task value", redacted)
        self.assertIn("display name: TOKENIZATION", redacted)
        self.assertIn("task name: PASSWORD RESET", redacted)
        self.assertIn("name: Password Reset", redacted)
        self.assertIn("name: Auth Migration", redacted)
        self.assertIn("name: API Key Rotation", redacted)
        self.assertIn("notes: document secret scanning behavior", redacted)
        self.assertNotIn("rotate visible docs", redacted)
        self.assertIn("visible before title", redacted)
        self.assertIn("visible auth task value after title", redacted)
        self.assertNotIn("api-rotation-split-secret", redacted)
        self.assertNotIn("hidden-api-rotation-split-secret", redacted)
        self.assertNotIn("punct-api-rotation-split-secret", redacted)
        self.assertNotIn("mixed-natural-title-secret", redacted)
        self.assertNotIn("punct-natural-title-secret", redacted)
        self.assertNotIn("cased-api-rotation-secret", redacted)
        self.assertNotIn("cased-auth-migration-secret", redacted)
        self.assertNotIn("cased-password-reset-secret", redacted)
        self.assertNotIn("cased-token-cleanup-secret", redacted)
        self.assertIn("compass: visible direction", redacted)

    def test_redacts_json_style_secrets_in_allowed_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "config.json").write_text(
                (
                    "{\n"
                    '  "api_key": "json-secret",\n'
                    '  "token": "token-secret",\n'
                    '  "auth": "auth-secret",\n'
                    '  "credentials": "json-credential-secret",\n'
                    '  "password": "abc\\"def",\n'
                    '  "tokens": ["array-secret"],\n'
                    '  "secret": {"value": "first-object-secret",\n'
                    '  "extra": "second-object-secret"},\n'
                    '  "token": {\n'
                    '    "marker": "secret } marker",\n'
                    '    "later": "after-marker-secret",\n'
                    '    "value": "nested-json-secret"\n'
                    "  },\n"
                    '  "secret": {\n'
                    '"value": "same-indent-secret"\n'
                    "},\n"
                    '  "secret": {\n'
                    "    # }\n"
                    '    "value": "comment-marker-secret"\n'
                    "  },\n"
                    '  "settings": { "secret": {\n'
                    '    "value": "midline-container-secret"\n'
                    "  }},\n"
                    '  "credentials":\n'
                    "{\n"
                    '"value": "split-secret"\n'
                    "},\n"
                    '  "ESCAPED_DATABASE_URL": "postgres:\\/\\/:escaped-db-pass@example\\/app",\n'
                    '  "ok": "yes"\n'
                    "}\n"
                ),
                encoding="utf-8",
            )
            (repo / "config.yaml").write_text(
                "environment: { POSTGRES_PASSWORD: inline-secret }\n"
                "DATABASE_URL: postgres://user:db-pass@example/app\n"
                "REDIS_URL: redis://:redis-secret@host/0\n"
                "client-key-data: kube-key-secret\n"
                "GIT_URL: https://ghp_tokenvalue@example.com/org/repo.git\n"
                "PAT_URL: https://github_pat_value@example.com/org/repo.git\n"
                "BASIC_PAT_URL: https://github_pat_value:x-oauth-basic@example.com/org/repo.git\n"
                "OLD_PAT_URL: https://gho_tokenvalue@example.com/org/repo.git\n"
                "OLD_BASIC_PAT_URL: https://ghs_tokenvalue:x-oauth-basic@example.com/org/repo.git\n"
                "public: visible\n",
                encoding="utf-8",
            )

            bundle = collect_generated_discovery_evidence(repo)

        content = "\n".join(
            file["content"] for file in bundle.prompt_input_json()["files"]
        )
        self.assertNotIn("json-secret", content)
        self.assertNotIn("token-secret", content)
        self.assertNotIn("auth-secret", content)
        self.assertNotIn("json-credential-secret", content)
        self.assertNotIn('abc\\"def', content)
        self.assertNotIn("array-secret", content)
        self.assertNotIn("first-object-secret", content)
        self.assertNotIn("second-object-secret", content)
        self.assertNotIn("after-marker-secret", content)
        self.assertNotIn("nested-json-secret", content)
        self.assertNotIn("same-indent-secret", content)
        self.assertNotIn("comment-marker-secret", content)
        self.assertNotIn("midline-container-secret", content)
        self.assertNotIn("split-secret", content)
        self.assertNotIn("escaped-db-pass", content)
        self.assertNotIn("inline-secret", content)
        self.assertNotIn("db-pass", content)
        self.assertNotIn("redis-secret", content)
        self.assertIn("redis://:<redacted>@host/0", content)
        self.assertNotIn("kube-key-secret", content)
        self.assertNotIn("ghp_tokenvalue", content)
        self.assertNotIn("github_pat_value", content)
        self.assertNotIn("gho_tokenvalue", content)
        self.assertNotIn("ghs_tokenvalue", content)
        self.assertNotIn("x-oauth-basic", content)
        self.assertIn("postgres://user:<redacted>@example/app", content)
        self.assertIn("postgres:\\/\\/:<redacted>@example\\/app", content)
        self.assertIn("https://<redacted>@example.com/org/repo.git", content)
        self.assertIn("https://<redacted>:<redacted>@example.com/org/repo.git", content)
        self.assertIn('"api_key": "<redacted>"', content)
        self.assertIn('"auth": "<redacted>"', content)
        self.assertIn('"ok": "yes"', content)
        self.assertIn("public: visible", content)

    def test_normalizes_configured_state_directory_before_skip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            state = repo / ".state" / "vibe-loop"
            state.mkdir(parents=True)
            (state / "generated-task-source.json").write_text("{}\n", encoding="utf-8")
            (repo / "PLAN.md").write_text("plan\n", encoding="utf-8")

            bundle = collect_generated_discovery_evidence(
                repo,
                state_dir=".state/../.state/vibe-loop",
            )

        self.assertEqual([file.path for file in bundle.files], ["PLAN.md"])
        self.assertIn(
            (".state/vibe-loop", "state_directory"),
            {(item.path, item.reason) for item in bundle.skipped},
        )

    def test_caps_skipped_evidence_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            for index in range(5):
                (repo / f"script-{index}.py").write_text("pass\n", encoding="utf-8")

            bundle = collect_generated_discovery_evidence(
                repo,
                limits=EvidenceLimits(max_skipped_entries=3),
            )

        skipped = bundle.manifest_json()["skipped"]
        self.assertEqual(len(skipped), 3)
        self.assertIn(
            {
                "path": ".",
                "reason": "skipped_manifest_limit",
                "detail": "3 additional skipped evidence entries omitted",
            },
            skipped,
        )

    def test_caps_collected_file_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            for index in range(4):
                (repo / f"note-{index}.md").write_text("", encoding="utf-8")

            bundle = collect_generated_discovery_evidence(
                repo,
                limits=EvidenceLimits(max_files=2),
            )

        self.assertEqual(len(bundle.files), 2)
        self.assertEqual(len(bundle.manifest_json()["files"]), 2)
        skipped = {(item.path, item.reason) for item in bundle.skipped}
        self.assertIn(("note-2.md", "file_count_limit"), skipped)
        self.assertIn(("note-3.md", "file_count_limit"), skipped)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "mkfifo unavailable")
    def test_skips_non_regular_allowed_files_without_reading(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "PLAN.md").write_text("plan\n", encoding="utf-8")
            os.mkfifo(repo / "pipe.md")

            bundle = collect_generated_discovery_evidence(repo)

        self.assertEqual([file.path for file in bundle.files], ["PLAN.md"])
        self.assertIn(
            ("pipe.md", "non_regular_file"),
            {(item.path, item.reason) for item in bundle.skipped},
        )

    def test_records_unreadable_directory_walk_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory).resolve()

            def fake_walk(
                top: str | bytes | PathLike[str] | PathLike[bytes],
                onerror=None,
            ) -> Iterator[tuple[str, list[str], list[str]]]:
                if onerror:
                    onerror(PermissionError(13, "Permission denied", str(repo / "x")))
                yield str(top), [], []

            with patch("vibe_loop.generated_discovery.os.walk", fake_walk):
                bundle = collect_generated_discovery_evidence(repo)

        self.assertEqual(
            bundle.manifest_json()["skipped"],
            [
                {
                    "path": "x",
                    "reason": "unreadable_directory",
                    "detail": "[Errno 13] Permission denied: './x'",
                }
            ],
        )

    def test_prompt_input_contains_skipped_evidence_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "PLAN.md").write_text("password = secret\n", encoding="utf-8")
            (repo / "image.png").write_bytes(b"png")

            bundle = collect_generated_discovery_evidence(repo)
            prompt = bundle.prompt_input_json()

        self.assertEqual(prompt["schema_version"], 1)
        self.assertEqual(prompt["manifest"]["repo"], ".")
        self.assertEqual(prompt["manifest"]["files"][0]["path"], "PLAN.md")
        self.assertTrue(prompt["manifest"]["files"][0]["redacted"])
        self.assertEqual(
            prompt["manifest"]["skipped"],
            [{"path": "image.png", "reason": "unsupported_file_type"}],
        )

    def test_skips_symlinks_to_keep_evidence_repo_local(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            outside = Path(directory) / "outside.md"
            repo.mkdir()
            outside.write_text("outside\n", encoding="utf-8")
            (repo / "PLAN.md").write_text("inside\n", encoding="utf-8")
            (repo / "linked.md").symlink_to(outside)

            bundle = collect_generated_discovery_evidence(repo)

        self.assertEqual([file.path for file in bundle.files], ["PLAN.md"])
        self.assertIn(
            ("linked.md", "symlink"),
            {(item.path, item.reason) for item in bundle.skipped},
        )

    def test_redacts_private_key_blocks(self) -> None:
        text = (
            "before\n"
            "-----BEGIN PRIVATE KEY-----\n"
            "secret-key-material\n"
            "-----END PRIVATE KEY-----\n"
            "-----BEGIN PGP PRIVATE KEY BLOCK-----\n"
            "pgp-secret-key-material\n"
            "-----END PGP PRIVATE KEY BLOCK-----\n"
            "after\n"
        )

        redacted = redact_evidence_text(text)

        self.assertNotIn("secret-key-material", redacted)
        self.assertNotIn("pgp-secret-key-material", redacted)
        self.assertIn("<redacted private key>", redacted)


if __name__ == "__main__":
    unittest.main()
