# Copyright 2026 The Bazel Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""apple_xctestrun_runner Starlark tests."""

load("@bazel_skylib//lib:unittest.bzl", "asserts")
load("//apple:providers.bzl", "AppleTestRunnerInfo")
load(
    "//test/starlark_tests/rules:analysis_provider_test.bzl",
    "make_provider_test_rule",
)

def _execution_environment(_ctx, provider):
    return provider.execution_environment

def _empty_xcode_version_execution_environment_impl(_ctx, env, execution_environment):
    asserts.equals(env, {}, execution_environment)

_apple_xctestrun_runner_empty_xcode_config_test = make_provider_test_rule(
    provider = AppleTestRunnerInfo,
    provider_fn = _execution_environment,
    assertion_fn = _empty_xcode_version_execution_environment_impl,
    attrs = {},
    config_settings = {
        "//command_line_option:platforms": [str(Label("@apple_support//platforms:macos_arm64"))],
        "//command_line_option:xcode_version_config": str(Label("//test/starlark_tests/targets_under_test/apple:empty_xcode_config")),
    },
)

def apple_xctestrun_runner_test_suite(name):
    """Test suite for apple_xctestrun_runner.

    Args:
      name: the base name to be used in things created by this macro
    """
    _apple_xctestrun_runner_empty_xcode_config_test(
        name = "{}_empty_xcode_version_execution_environment".format(name),
        target_under_test = "//test/starlark_tests/targets_under_test/apple:apple_xctestrun_runner_empty_xcode_config",
        tags = [name],
    )
