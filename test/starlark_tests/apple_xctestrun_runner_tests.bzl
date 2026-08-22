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
load("//apple:providers.bzl", "AppleDeviceTestRunnerInfo", "AppleTestRunnerInfo")
load(
    "//test/starlark_tests/rules:analysis_provider_test.bzl",
    "make_provider_test_rule",
)

def _execution_environment(_ctx, provider):
    return provider.execution_environment

def _device_runner_info(_ctx, provider):
    return struct(
        device_type = provider.device_type,
        os_version = provider.os_version,
    )

def _empty_xcode_version_execution_environment_impl(_ctx, env, execution_environment):
    asserts.equals(env, {}, execution_environment)

def _ios_simulator_settings_device_info_impl(_ctx, env, device_info):
    asserts.equals(env, "iPhone 15", device_info.device_type)
    asserts.equals(env, "17.2", device_info.os_version)

def _non_ios_simulator_settings_device_info_impl(_ctx, env, device_info):
    asserts.equals(env, "", device_info.device_type)
    asserts.equals(env, "", device_info.os_version)

_apple_xctestrun_runner_empty_xcode_config_test = make_provider_test_rule(
    provider = AppleTestRunnerInfo,
    provider_fn = _execution_environment,
    assertion_fn = _empty_xcode_version_execution_environment_impl,
    attrs = {},
    config_settings = {
        "//command_line_option:apple_platform_type": "macos",
        "//command_line_option:platforms": str(Label("@apple_support//platforms:macos_arm64")),
        "//command_line_option:xcode_version_config": str(Label("//test/starlark_tests/targets_under_test/apple:empty_xcode_config")),
    },
)

_apple_xctestrun_runner_ios_simulator_settings_test = make_provider_test_rule(
    provider = AppleDeviceTestRunnerInfo,
    provider_fn = _device_runner_info,
    assertion_fn = _ios_simulator_settings_device_info_impl,
    attrs = {},
    config_settings = {
        "//command_line_option:apple_platform_type": "ios",
        "//command_line_option:platforms": str(Label("@apple_support//platforms:ios_sim_arm64")),
        str(Label("//apple/build_settings:ios_simulator_device")): "iPhone 15",
        str(Label("//apple/build_settings:ios_simulator_version")): "17.2",
    },
)

_apple_xctestrun_runner_tvos_simulator_settings_test = make_provider_test_rule(
    provider = AppleDeviceTestRunnerInfo,
    provider_fn = _device_runner_info,
    assertion_fn = _non_ios_simulator_settings_device_info_impl,
    attrs = {},
    config_settings = {
        "//command_line_option:apple_platform_type": "tvos",
        "//command_line_option:platforms": str(Label("@apple_support//platforms:tvos_sim_arm64")),
        str(Label("//apple/build_settings:ios_simulator_device")): "iPhone 15",
        str(Label("//apple/build_settings:ios_simulator_version")): "17.2",
    },
)

_apple_xctestrun_runner_visionos_simulator_settings_test = make_provider_test_rule(
    provider = AppleDeviceTestRunnerInfo,
    provider_fn = _device_runner_info,
    assertion_fn = _non_ios_simulator_settings_device_info_impl,
    attrs = {},
    config_settings = {
        "//command_line_option:apple_platform_type": "visionos",
        "//command_line_option:platforms": str(Label("@apple_support//platforms:visionos_sim_arm64")),
        str(Label("//apple/build_settings:ios_simulator_device")): "iPhone 15",
        str(Label("//apple/build_settings:ios_simulator_version")): "17.2",
    },
)

_apple_xctestrun_runner_watchos_simulator_settings_test = make_provider_test_rule(
    provider = AppleDeviceTestRunnerInfo,
    provider_fn = _device_runner_info,
    assertion_fn = _non_ios_simulator_settings_device_info_impl,
    attrs = {},
    config_settings = {
        "//command_line_option:apple_platform_type": "watchos",
        "//command_line_option:platforms": str(Label("@apple_support//platforms:watchos_x86_64")),
        str(Label("//apple/build_settings:ios_simulator_device")): "iPhone 15",
        str(Label("//apple/build_settings:ios_simulator_version")): "17.2",
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
    _apple_xctestrun_runner_ios_simulator_settings_test(
        name = "{}_ios_simulator_settings".format(name),
        target_under_test = "//test/starlark_tests/targets_under_test/apple:apple_xctestrun_runner_platform_config",
        tags = [name],
    )
    _apple_xctestrun_runner_tvos_simulator_settings_test(
        name = "{}_tvos_ignores_ios_simulator_settings".format(name),
        target_under_test = "//test/starlark_tests/targets_under_test/apple:apple_xctestrun_runner_platform_config",
        tags = [name],
    )
    _apple_xctestrun_runner_visionos_simulator_settings_test(
        name = "{}_visionos_ignores_ios_simulator_settings".format(name),
        target_under_test = "//test/starlark_tests/targets_under_test/apple:apple_xctestrun_runner_platform_config",
        tags = [name],
    )
    _apple_xctestrun_runner_watchos_simulator_settings_test(
        name = "{}_watchos_ignores_ios_simulator_settings".format(name),
        target_under_test = "//test/starlark_tests/targets_under_test/apple:apple_xctestrun_runner_platform_config",
        tags = [name],
    )
