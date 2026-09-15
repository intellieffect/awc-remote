"""tests.functional.utils is imported for the skip/fail rule alone: it needs
no server and starts no reactor.
"""
from __future__ import annotations

import unittest
from typing import Dict, List, Mapping, Tuple

from tests.functional.utils import (
    OS_SERVERS_BY_PLATFORM,
    TCP_SERVERS,
    VNCEV,
    VNCServer,
    absent_server_skips,
    hosted_isolated_ci,
    os_servers,
    running_in_ci,
)

HOSTED = {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted"}

# CI-looking environments that are still not a throwaway hosted runner.
NOT_HOSTED: Dict[str, Mapping[str, str]] = {
    "unset": {},
    "CI_true_alone": {"CI": "true"},
    "GITHUB_ACTIONS_alone": {"GITHUB_ACTIONS": "true"},
    "self_hosted": {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "self-hosted"},
    "hosted_but_not_ci": {"RUNNER_ENVIRONMENT": "github-hosted"},
}

ENV_SAMPLES: Dict[str, Tuple[Mapping[str, str], bool]] = {
    "unset": ({}, False),
    "CI_empty": ({"CI": ""}, False),
    "CI_false": ({"CI": "false"}, False),
    "CI_0": ({"CI": "0"}, False),
    "GITHUB_ACTIONS_false": ({"GITHUB_ACTIONS": "false"}, False),
    "CI_true": ({"CI": "true"}, True),
    "CI_1": ({"CI": "1"}, True),
    "GITHUB_ACTIONS_true": ({"GITHUB_ACTIONS": "true"}, True),
    "both": ({"CI": "true", "GITHUB_ACTIONS": "true"}, True),
}

# Linux's OS-hosted server is registered only when the QEMU setup script has
# exported its opt-in, so os_servers("linux") is empty here and there is
# nothing to generate a case from.
NATIVE_PLATFORMS = ("darwin", "win32")

NEVER_SKIPPING = TCP_SERVERS + [VNCEV]


def native_servers() -> List[Tuple[str, VNCServer]]:
    return [(platform, server) for platform in NATIVE_PLATFORMS for server in os_servers(platform, HOSTED)]


class CIDetection:
    env: Mapping[str, str]
    is_ci: bool

    def test_says_whether_this_is_ci(self) -> None:
        self.assertEqual(running_in_ci(self.env), self.is_ci)  # type: ignore[attr-defined]


class NativeServerVerdict:
    server: VNCServer
    env: Mapping[str, str]
    is_ci: bool

    def test_skips_only_off_ci(self) -> None:
        self.assertEqual(  # type: ignore[attr-defined]
            absent_server_skips(self.server, self.env),
            not self.is_ci,
            f"{self.server.name} absent: a CI run must report a failure, a "
            "developer's run a skip",
        )


class ContainerServerVerdict:
    server: VNCServer
    env: Mapping[str, str]

    def test_never_skips(self) -> None:
        self.assertFalse(  # type: ignore[attr-defined]
            absent_server_skips(self.server, self.env),
            f"{self.server.name} is started by `make servers-up`, so a run "
            "without it must fail rather than pass as green",
        )


class TestNativeServersAreRegistered(unittest.TestCase):
    def test_every_supported_platform_has_one_on_a_hosted_runner(self) -> None:
        for platform in NATIVE_PLATFORMS:
            self.assertTrue(
                os_servers(platform, HOSTED),
                f"no OS-hosted server registered for {platform}, so the cases "
                "generated below assert nothing",
            )
            self.assertEqual(os_servers(platform, HOSTED), OS_SERVERS_BY_PLATFORM[platform])


class TestNativeServersAreNotRegisteredElsewhere(unittest.TestCase):
    """Fail closed: off a hosted runner nothing native registers, whatever 5900 answers."""

    def test_hosted_gate(self) -> None:
        self.assertTrue(hosted_isolated_ci(HOSTED))
        for name, env in NOT_HOSTED.items():
            with self.subTest(env=name):
                self.assertFalse(hosted_isolated_ci(env))

    def test_nothing_registers_off_a_hosted_runner(self) -> None:
        for platform in NATIVE_PLATFORMS:
            for name, env in NOT_HOSTED.items():
                with self.subTest(platform=platform, env=name):
                    self.assertEqual(os_servers(platform, env), [])

    def test_this_very_process_registers_nothing_unless_hosted(self) -> None:
        import os

        if hosted_isolated_ci(os.environ):
            self.skipTest("on a hosted runner, where registration is the point")
        self.assertEqual(os_servers(), [])


def _case(name: str, body: type, attrs: Dict[str, object], method: str) -> unittest.TestCase:
    return type(name, (body, unittest.TestCase), attrs)(method)


def load_tests(loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: object) -> unittest.TestSuite:
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestNativeServersAreRegistered))
    suite.addTests(loader.loadTestsFromTestCase(TestNativeServersAreNotRegisteredElsewhere))
    for env_name, (env, is_ci) in ENV_SAMPLES.items():
        suite.addTest(
            _case(
                f"TestCIDetection_{env_name}",
                CIDetection,
                {"env": env, "is_ci": is_ci},
                "test_says_whether_this_is_ci",
            )
        )
        for platform, server in native_servers():
            suite.addTest(
                _case(
                    f"TestNativeVerdict_{platform}_{server.name.replace('-', '_')}_{env_name}",
                    NativeServerVerdict,
                    {"server": server, "env": env, "is_ci": is_ci},
                    "test_skips_only_off_ci",
                )
            )
        for server in NEVER_SKIPPING:
            suite.addTest(
                _case(
                    f"TestContainerVerdict_{server.name.replace('-', '_')}_{env_name}",
                    ContainerServerVerdict,
                    {"server": server, "env": env},
                    "test_never_skips",
                )
            )
    return suite


if __name__ == "__main__":
    unittest.main()
