#!/usr/bin/python3
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
"""Runs Apple XCTest bundles using xctestrun files or direct xctest."""

import argparse
import collections
import glob
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile


_PLATFORM_DISPLAY_NAMES = {
    "ios": "iOS",
    "tvos": "tvOS",
    "macos": "macOS",
    "visionos": "visionOS",
    "watchos": "watchOS",
}

_SIMULATOR_PLATFORM_DIRECTORIES = {
    "ios": "iPhoneSimulator.platform",
    "tvos": "AppleTVSimulator.platform",
    "macos": "MacOSX.platform",
    "visionos": "XROSSimulator.platform",
    "watchos": "WatchSimulator.platform",
}

_DEVICE_PLATFORM_DIRECTORIES = {
    "ios": "iPhoneOS.platform",
    "tvos": "AppleTVOS.platform",
    "macos": "MacOSX.platform",
    "visionos": "XROS.platform",
    "watchos": "WatchOS.platform",
}

_SDK_VERSION_CONFIG_KEYS = {
    "ios": "ios_sdk_version",
    "tvos": "tvos_sdk_version",
    "macos": "macos_sdk_version",
    "visionos": "visionos_sdk_version",
    "watchos": "watchos_sdk_version",
}


class _RunnerError(Exception):
  """Exception used for expected runner failures."""

  def __init__(self, message=None, exit_code=1):
    super().__init__(message)
    self.message = message
    self.exit_code = exit_code


def _basename_without_extension(path):
  return os.path.splitext(os.path.basename(path))[0]


def _bool_from_config(value):
  if isinstance(value, bool):
    return value
  return str(value).lower() == "true"


def _check_output(args, env=None):
  return subprocess.check_output(args, env=env, text=True).strip()


def _copy_directory(src, dst):
  if os.path.exists(dst):
    shutil.rmtree(dst)
  shutil.copytree(src, dst, symlinks=False)


def _copy_file(src, dst):
  os.makedirs(os.path.dirname(dst), exist_ok=True)
  shutil.copy2(src, dst)


def _chmod_recursive(path, mode):
  for root, dirs, files in os.walk(path):
    os.chmod(root, mode)
    for name in dirs:
      os.chmod(os.path.join(root, name), mode)
    for name in files:
      os.chmod(os.path.join(root, name), mode)


def _is_non_empty_file(path):
  return bool(path and os.path.exists(path) and os.path.getsize(path) > 0)


def _read_text_file(path):
  with open(path, encoding="utf-8", errors="replace") as file_obj:
    return file_obj.read()


def _run_check(args, env=None):
  result = subprocess.run(args, env=env, check=False)
  if result.returncode:
    raise _RunnerError(exit_code=result.returncode)


def _split_command_line_args(values):
  result = []
  for value in values:
    if not value:
      continue
    result.extend([part for part in value.split(",") if part])
  return result


def _stream_command_to_log(args, testlog, env=None):
  with open(testlog, "wb") as testlog_file:
    process = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
    )
    assert process.stdout
    while True:
      chunk = process.stdout.readline()
      if not chunk:
        break
      sys.stdout.buffer.write(chunk)
      sys.stdout.buffer.flush()
      testlog_file.write(chunk)
      testlog_file.flush()
    return process.wait()


def _write_plist(path, value):
  with open(path, "wb") as file_obj:
    plistlib.dump(value, file_obj, fmt=plistlib.FMT_XML, sort_keys=False)


class _AppleXctestrunRunner(object):
  """Default Apple XCTest runner implementation."""

  def __init__(self, runner_config, test_config, args):
    self.runner_config = runner_config
    self.test_config = test_config

    self.create_xcresult_bundle = _bool_from_config(
        runner_config["create_xcresult_bundle"],
    )
    if os.environ.get("CREATE_XCRESULT_BUNDLE"):
      self.create_xcresult_bundle = True

    self.test_platform_type = runner_config["test_platform_type"]
    self.test_minimum_os_version = test_config["test_minimum_os_version"]
    self.custom_xcodebuild_args = list(runner_config.get("xcodebuild_args", []))
    self.command_line_args = list(runner_config.get("command_line_args", []))
    self.attachment_lifetime = runner_config["attachment_lifetime"]
    self.screen_capture_format = runner_config["screen_capture_format"]
    self.destination_timeout = runner_config["destination_timeout"]
    self.device_id = ""
    self.destination_platform = ""

    self.test_tmp_dir = None
    self.profraw = None
    self.test_bundle_path = ""
    self.test_bundle_name = ""
    self.test_bundle_binary = ""
    self.test_host_path = ""
    self.test_host_name = ""
    self.test_execution_platform = ""
    self.xcodebuild_destination_platform = ""
    self.build_for_device = False
    self.xctestrun_env = collections.OrderedDict()
    self.passthrough_env = {}
    self.xctestrun_test_host_path = ""
    self.xctestrun_test_host_based = False
    self.xcrun_target_app_path = ""
    self.xcrun_test_host_bundle_identifier = ""
    self.xcrun_test_bundle_path = ""
    self.xcrun_is_xctrunner_hosted_bundle = False
    self.xcrun_is_ui_test_bundle = False
    self.sanitizer_dyld_env = ""
    self.xctestrun_libraries = ""
    self.xctestrun_platform_environment = collections.OrderedDict()
    self.skip_tests = []
    self.only_tests = []
    self.reuse_simulator = ""
    self.simulator_id = "unused"
    self.intel_simulator_hack = False
    self.architecture = "arm64"
    self.should_use_xcodebuild = False
    self.test_exit_code = 0
    self.testlog = ""
    self.result_bundle_path = ""
    self.llvm_cov_status = 0
    self.llvm_cov_json_export_status = 0
    self.post_action_exit_code = 0

    self._parse_args(args)

  def _die(self, message, exit_code=1):
    print(message, file=sys.stderr)
    raise _RunnerError(exit_code=exit_code)

  def _parse_args(self, args):
    for arg in args:
      if arg.startswith("--xcodebuild_args="):
        self.custom_xcodebuild_args.append(arg[len("--xcodebuild_args="):])
      elif arg.startswith("--destination=platform=") and ",id=" in arg:
        destination = arg[len("--destination="):]
        destination_platform = destination[len("platform="):]
        self.destination_platform = destination_platform.split(",id=", 1)[0]
        self.device_id = destination.rsplit(",id=", 1)[1]
      elif arg.startswith("--command_line_args="):
        self.command_line_args.append(arg.rsplit("=", 1)[1])
      elif arg.startswith("--xctestrun_attachment_lifetime="):
        self.attachment_lifetime = arg.rsplit("=", 1)[1]
      elif arg.startswith("--xctestrun_screen_capture_format="):
        self.screen_capture_format = arg.rsplit("=", 1)[1]
      else:
        self._die("error: Unsupported argument '{}'".format(arg))

  def run(self):
    self._prepare_work_dir()
    try:
      self._stage_test_bundle()
      self._select_execution_platform()
      self._stage_test_host()
      self._build_test_environment()
      self._configure_test_host()
      self._configure_injected_libraries()
      self._configure_platform_environment()
      self._build_test_filters()
      self._create_simulator()
      self._choose_execution_strategy()
      self._run_pre_action()
      self._run_tests()
      self._collect_coverage()
      self._run_post_action()
      self._fail_for_post_action_before_cleanup()
      self._discard_xcresult_on_success()
      self._clean_up_simulator()
      self._fail_for_test_or_post_action()
      self._verify_tests_ran()
      self._verify_no_false_negatives()
      self._verify_coverage_exports()
      self._mark_test_complete()
      return 0
    finally:
      self._remove_work_dir()

  def _platform_display_name(self):
    try:
      return _PLATFORM_DISPLAY_NAMES[self.test_platform_type]
    except KeyError:
      self._die(
          "error: unsupported Apple test platform '{}'".format(
              self.test_platform_type,
          ),
      )

  def _simulator_platform_directory_name(self):
    try:
      return _SIMULATOR_PLATFORM_DIRECTORIES[self.test_platform_type]
    except KeyError:
      self._die(
          "error: unsupported Apple simulator platform '{}'".format(
              self.test_platform_type,
          ),
      )

  def _device_platform_directory_name(self):
    try:
      return _DEVICE_PLATFORM_DIRECTORIES[self.test_platform_type]
    except KeyError:
      self._die(
          "error: unsupported Apple device platform '{}'".format(
              self.test_platform_type,
          ),
      )

  def _platform_sdk_version(self):
    try:
      return self.runner_config[_SDK_VERSION_CONFIG_KEYS[self.test_platform_type]]
    except KeyError:
      self._die(
          "error: unsupported Apple SDK platform '{}'".format(
              self.test_platform_type,
          ),
      )

  def _prepare_work_dir(self):
    tmp_root = os.environ.get("TEST_TMPDIR") or os.environ.get("TMPDIR") or "/tmp"
    if os.environ.get("NO_CLEAN"):
      self.test_tmp_dir = os.path.join(
          os.environ.get("TMPDIR") or "/tmp",
          "test_tmp_dir",
      )
      shutil.rmtree(self.test_tmp_dir, ignore_errors=True)
      os.makedirs(self.test_tmp_dir)
      print("note: keeping test dir around at: {}".format(self.test_tmp_dir))
    else:
      self.test_tmp_dir = tempfile.mkdtemp(prefix="test_tmp_dir.", dir=tmp_root)
    self.profraw = os.path.join(self.test_tmp_dir, "coverage.profraw")

  def _remove_work_dir(self):
    if self.test_tmp_dir and not os.environ.get("NO_CLEAN"):
      shutil.rmtree(self.test_tmp_dir, ignore_errors=True)

  def _stage_test_bundle(self):
    self.test_bundle_path = self.test_config["test_bundle_path"]
    self.test_bundle_name = _basename_without_extension(self.test_bundle_path)
    self.test_bundle_binary = os.path.join(
        self.test_tmp_dir,
        "{}.xctest".format(self.test_bundle_name),
        self.test_bundle_name,
    )
    if self.test_platform_type == "macos":
      self.test_bundle_binary = os.path.join(
          self.test_tmp_dir,
          "{}.xctest".format(self.test_bundle_name),
          "Contents",
          "MacOS",
          self.test_bundle_name,
      )

    if self.test_bundle_path.endswith(".xctest"):
      destination = os.path.join(
          self.test_tmp_dir,
          "{}.xctest".format(self.test_bundle_name),
      )
      _copy_directory(self.test_bundle_path, destination)
      _chmod_recursive(destination, 0o777)
    else:
      with zipfile.ZipFile(self.test_bundle_path) as archive:
        archive.extractall(self.test_tmp_dir)

    open(self.test_bundle_binary, "a").close()
    os.utime(self.test_bundle_binary, None)

  def _select_execution_platform(self):
    self.build_for_device = False
    self.test_execution_platform = self._simulator_platform_directory_name()
    self.xcodebuild_destination_platform = self._platform_display_name()
    if self.test_platform_type == "macos":
      self.test_execution_platform = "MacOSX.platform"
      self.xcodebuild_destination_platform = "macOS"
      return

    if self.device_id:
      self.test_execution_platform = self._device_platform_directory_name()
      self.xcodebuild_destination_platform = (
          self.destination_platform or self.xcodebuild_destination_platform
      )
      self.build_for_device = True

  def _stage_test_host(self):
    self.test_host_path = self.test_config["test_host_path"]
    if not self.test_host_path:
      return

    self.test_host_name = _basename_without_extension(self.test_host_path)
    if self.test_host_path.endswith(".app"):
      destination = os.path.join(
          self.test_tmp_dir,
          "{}.app".format(self.test_host_name),
      )
      _copy_directory(self.test_host_path, destination)
      _chmod_recursive(destination, 0o777)
      return

    with zipfile.ZipFile(self.test_host_path) as archive:
      archive.extractall(self.test_tmp_dir)
    if self.test_platform_type == "macos":
      return

    payload_path = os.path.join(self.test_tmp_dir, "Payload")
    for app_name in os.listdir(payload_path):
      if app_name.endswith(".app"):
        shutil.move(
            os.path.join(payload_path, app_name),
            os.path.join(self.test_tmp_dir, app_name),
        )
        break

    for child in os.listdir(self.test_tmp_dir):
      child_path = os.path.join(self.test_tmp_dir, child)
      if child.endswith(".app") and os.path.isdir(child_path):
        self.test_host_name = _basename_without_extension(child_path)
        break

  def _build_test_environment(self):
    default_test_env = collections.OrderedDict([
        ("TEST_PREMATURE_EXIT_FILE", os.environ.get("TEST_PREMATURE_EXIT_FILE", "")),
        ("TEST_SRCDIR", os.environ.get("TEST_SRCDIR", "")),
        (
            "TEST_UNDECLARED_OUTPUTS_DIR",
            os.environ.get("TEST_UNDECLARED_OUTPUTS_DIR", ""),
        ),
        ("XML_OUTPUT_FILE", os.environ.get("XML_OUTPUT_FILE", "")),
    ])
    test_env = collections.OrderedDict(self.test_config.get("test_env", {}))

    for env_var in self.test_config.get("test_env_inherit", []):
      if env_var in os.environ:
        test_env[env_var] = os.environ[env_var]

    if self.device_id:
      default_test_env["BAZEL_DEVICE_UDID"] = self.device_id
    if self.test_platform_type == "macos" and os.environ.get("COVERAGE") == "1":
      default_test_env["LLVM_PROFILE_FILE"] = self.profraw

    test_env.update(default_test_env)
    self.xctestrun_env = test_env
    self.passthrough_env = {
        "SIMCTL_CHILD_{}".format(key): value
        for key, value in self.xctestrun_env.items()
    }

  def _configure_test_host(self):
    self.xcrun_target_app_path = ""
    self.xcrun_test_host_bundle_identifier = ""
    self.xcrun_test_bundle_path = "__TESTROOT__/{}.xctest".format(
        self.test_bundle_name,
    )
    self.xcrun_is_xctrunner_hosted_bundle = False
    self.xcrun_is_ui_test_bundle = False
    test_type = self.test_config["test_type"]

    if self.test_platform_type == "macos":
      if test_type == "XCUITEST":
        print("This runner only works with macos_unit_test (b/63707899).")
        raise _RunnerError(exit_code=1)

      if self.test_host_path:
        self.xctestrun_test_host_path = "__TESTROOT__/{}.app".format(
            self.test_host_name,
        )
        self.xctestrun_test_host_based = True
        self.xctestrun_env["XCInjectBundleInto"] = (
            "__TESTHOST__/Contents/MacOS/{}".format(self.test_host_name)
        )
      else:
        self.xctestrun_test_host_path = (
            "__PLATFORMS__/MacOSX.platform/Developer/Library/Xcode/Agents/xctest"
        )
        self.xctestrun_test_host_based = False
      return

    if not self.test_host_path:
      self.xctestrun_test_host_path = (
          "__PLATFORMS__/{}/Developer/Library/Xcode/Agents/xctest".format(
              self.test_execution_platform,
          )
      )
      self.xctestrun_test_host_based = False
      return

    developer_dir = _check_output(["xcode-select", "-p"])
    self.xctestrun_test_host_path = "__TESTROOT__/{}.app".format(
        self.test_host_name,
    )
    self.xctestrun_test_host_based = True
    self.xctestrun_env["XCInjectBundleInto"] = "__TESTHOST__/{0}.app/{0}".format(
        self.test_host_name,
    )

    developer_path = os.path.join(
        developer_dir,
        "Platforms",
        self.test_execution_platform,
        "Developer",
    )
    libraries_path = os.path.join(developer_path, "Library")
    testing_framework_path = os.path.join(
        libraries_path,
        "Frameworks",
        "Testing.framework",
    )
    if os.path.isdir(testing_framework_path):
      self.xctestrun_env["DYLD_FRAMEWORK_PATH"] = os.path.join(
          libraries_path,
          "Frameworks",
      )

    if test_type != "XCUITEST":
      return

    self.xcrun_is_xctrunner_hosted_bundle = True
    self.xcrun_is_ui_test_bundle = True
    self.xcrun_target_app_path = self.xctestrun_test_host_path
    runner_app_name = "{}-Runner".format(self.test_bundle_name)
    runner_app = "{}.app".format(runner_app_name)
    runner_app_destination = os.path.join(self.test_tmp_dir, runner_app)
    _copy_directory(
        os.path.join(libraries_path, "Xcode", "Agents", "XCTRunner.app"),
        runner_app_destination,
    )
    _chmod_recursive(runner_app_destination, 0o777)
    self.xctestrun_test_host_path = "__TESTROOT__/{}".format(runner_app)
    self.xcrun_test_host_bundle_identifier = "com.apple.test.{}".format(
        runner_app_name,
    )
    plugins_path = os.path.join(self.test_tmp_dir, runner_app, "PlugIns")
    os.makedirs(plugins_path, exist_ok=True)
    shutil.move(
        os.path.join(self.test_tmp_dir, "{}.xctest".format(self.test_bundle_name)),
        plugins_path,
    )
    self.test_bundle_binary = os.path.join(
        plugins_path,
        "{}.xctest".format(self.test_bundle_name),
        self.test_bundle_name,
    )
    test_bundle_frameworks = os.path.join(
        plugins_path,
        "{}.xctest".format(self.test_bundle_name),
        "Frameworks",
    )
    os.makedirs(test_bundle_frameworks, exist_ok=True)
    if self.test_platform_type == "ios":
      libswift_concurrency_path = os.path.join(
          developer_dir,
          "Platforms",
          self._device_platform_directory_name(),
          "Library",
          "Developer",
          "CoreSimulator",
          "Profiles",
          "Runtimes",
          "iOS.simruntime",
          "Contents",
          "Resources",
          "RuntimeRoot",
          "usr",
          "lib",
          "swift",
          "libswift_Concurrency.dylib",
      )
      if os.path.exists(libswift_concurrency_path):
        _copy_file(
            libswift_concurrency_path,
            os.path.join(test_bundle_frameworks, "libswift_Concurrency.dylib"),
        )
    self.xcrun_test_bundle_path = "__TESTHOST__/PlugIns/{}.xctest".format(
        self.test_bundle_name,
    )

    runner_app_infoplist = os.path.join(runner_app_destination, "Info.plist")
    _run_check(["/usr/bin/plutil", "-convert", "xml1", runner_app_infoplist])
    info_plist = _read_text_file(runner_app_infoplist)
    info_plist = info_plist.replace("$(WRAPPEDPRODUCTNAME)", "XCTRunner")
    info_plist = info_plist.replace("WRAPPEDPRODUCTNAME", "XCTRunner")
    info_plist = info_plist.replace(
        "$(WRAPPEDPRODUCTBUNDLEIDENTIFIER)",
        self.xcrun_test_host_bundle_identifier,
    )
    info_plist = info_plist.replace(
        "WRAPPEDPRODUCTBUNDLEIDENTIFIER",
        self.xcrun_test_host_bundle_identifier,
    )
    with open(runner_app_infoplist, "w", encoding="utf-8") as file_obj:
      file_obj.write(info_plist)
    _run_check(["/usr/bin/plutil", "-convert", "binary1", runner_app_infoplist])

    runner_app_frameworks_destination = os.path.join(
        runner_app_destination,
        "Frameworks",
    )
    os.makedirs(runner_app_frameworks_destination, exist_ok=True)
    _copy_directory(
        os.path.join(libraries_path, "Frameworks", "XCTest.framework"),
        os.path.join(runner_app_frameworks_destination, "XCTest.framework"),
    )
    _copy_directory(
        os.path.join(libraries_path, "PrivateFrameworks", "XCTestCore.framework"),
        os.path.join(runner_app_frameworks_destination, "XCTestCore.framework"),
    )
    _copy_directory(
        os.path.join(
            libraries_path,
            "PrivateFrameworks",
            "XCTAutomationSupport.framework",
        ),
        os.path.join(
            runner_app_frameworks_destination,
            "XCTAutomationSupport.framework",
        ),
    )
    _copy_directory(
        os.path.join(libraries_path, "PrivateFrameworks", "XCUnit.framework"),
        os.path.join(runner_app_frameworks_destination, "XCUnit.framework"),
    )
    _copy_file(
        os.path.join(developer_path, "usr", "lib", "libXCTestSwiftSupport.dylib"),
        os.path.join(
            runner_app_frameworks_destination,
            "libXCTestSwiftSupport.dylib",
        ),
    )
    _copy_file(
        os.path.join(developer_path, "usr", "lib", "libXCTestBundleInject.dylib"),
        os.path.join(
            runner_app_frameworks_destination,
            "libXCTestBundleInject.dylib",
        ),
    )
    xctestsupport_framework_path = os.path.join(
        libraries_path,
        "PrivateFrameworks",
        "XCTestSupport.framework",
    )
    if os.path.isdir(xctestsupport_framework_path):
      _copy_directory(
          xctestsupport_framework_path,
          os.path.join(runner_app_frameworks_destination, "XCTestSupport.framework"),
      )
    if os.path.isdir(testing_framework_path):
      _copy_directory(
          testing_framework_path,
          os.path.join(runner_app_frameworks_destination, "Testing.framework"),
      )

    xcuiautomation_path = os.path.join(
        libraries_path,
        "Frameworks",
        "XCUIAutomation.framework",
    )
    if not os.path.isdir(xcuiautomation_path):
      xcuiautomation_path = os.path.join(
          libraries_path,
          "PrivateFrameworks",
          "XCUIAutomation.framework",
      )
    _copy_directory(
        xcuiautomation_path,
        os.path.join(runner_app_frameworks_destination, "XCUIAutomation.framework"),
    )

    if self.build_for_device:
      _run_check([
          "/usr/bin/lipo",
          os.path.join(self.test_tmp_dir, runner_app, "XCTRunner"),
          "-remove",
          "arm64e",
          "-output",
          os.path.join(self.test_tmp_dir, runner_app, "XCTRunner"),
      ])

    test_host_mobileprovision_path = os.path.join(
        self.test_tmp_dir,
        "{}.app".format(self.test_host_name),
        "embedded.mobileprovision",
    )
    if os.path.isfile(test_host_mobileprovision_path):
      self._sign_xctrunner(
          runner_app=runner_app,
          runner_app_destination=runner_app_destination,
          plugins_path=plugins_path,
      )

  def _sign_xctrunner(self, runner_app, runner_app_destination, plugins_path):
    test_host_mobileprovision_path = os.path.join(
        self.test_tmp_dir,
        "{}.app".format(self.test_host_name),
        "embedded.mobileprovision",
    )
    _copy_file(
        test_host_mobileprovision_path,
        os.path.join(self.test_tmp_dir, runner_app, "embedded.mobileprovision"),
    )
    xctrunner_entitlements = os.path.join(
        self.test_tmp_dir,
        runner_app,
        "RunnerEntitlements.plist",
    )
    test_host_binary_path = os.path.join(
        self.test_tmp_dir,
        "{}.app".format(self.test_host_name),
        self.test_host_name,
    )
    codesign_output = subprocess.check_output(
        ["codesign", "-dvv", test_host_binary_path],
        stderr=subprocess.STDOUT,
        text=True,
    )
    codesigning_team_identifier = re.search(
        r"TeamIdentifier=(.*)",
        codesign_output,
    ).group(1)
    codesigning_authority = re.search(
        r"^Authority=(.*)",
        codesign_output,
        re.MULTILINE,
    ).group(1)

    entitlements = _read_text_file(
        self.runner_config["xctrunner_entitlements_template"],
    )
    entitlements = entitlements.replace(
        "BAZEL_CODESIGNING_TEAM_IDENTIFIER",
        codesigning_team_identifier,
    )
    entitlements = entitlements.replace(
        "BAZEL_TEST_HOST_BUNDLE_IDENTIFIER",
        self.xcrun_test_host_bundle_identifier,
    )
    with open(xctrunner_entitlements, "w", encoding="utf-8") as file_obj:
      file_obj.write(entitlements)

    test_bundle_path = os.path.join(
        plugins_path,
        "{}.xctest".format(self.test_bundle_name),
    )
    _run_check([
        "codesign",
        "-f",
        "--entitlements",
        xctrunner_entitlements,
        "--timestamp=none",
        "-s",
        codesigning_authority,
        test_bundle_path,
    ])
    frameworks_dir = os.path.join(runner_app_destination, "Frameworks")
    for framework in glob.glob(os.path.join(frameworks_dir, "*.framework")):
      if os.path.isdir(framework):
        _run_check([
            "codesign",
            "-f",
            "--timestamp=none",
            "-s",
            codesigning_authority,
            "--entitlements",
            xctrunner_entitlements,
            framework,
        ])
    for dylib in glob.glob(os.path.join(frameworks_dir, "*.dylib")):
      if os.path.isfile(dylib):
        _run_check([
            "codesign",
            "-f",
            "--timestamp=none",
            "-s",
            codesigning_authority,
            "--entitlements",
            xctrunner_entitlements,
            dylib,
        ])
    _run_check([
        "codesign",
        "-f",
        "--entitlements",
        xctrunner_entitlements,
        "--timestamp=none",
        "-s",
        codesigning_authority,
        runner_app_destination,
    ])

  def _configure_injected_libraries(self):
    if self.test_platform_type == "macos":
      self.xctestrun_libraries = self.runner_config[
          "macos_xctestrun_insert_libraries"
      ]
      return

    test_bundle_frameworks = os.path.join(
        os.path.dirname(self.test_bundle_binary),
        "Frameworks",
    )
    sanitizers = [
        sanitizer
        for sanitizer in glob.glob(
            os.path.join(test_bundle_frameworks, "libclang_rt.*.dylib"),
        )
        if os.path.exists(sanitizer)
    ]
    self.sanitizer_dyld_env = ":".join(sanitizers)

    main_thread_checker_path = os.path.join(
        test_bundle_frameworks,
        "libMainThreadChecker.dylib",
    )
    main_thread_checker_dyld_env = ""
    if os.path.exists(main_thread_checker_path):
      main_thread_checker_dyld_env = main_thread_checker_path

    libraries = []
    if self.test_config["test_type"] != "XCUITEST":
      libraries.append(
          "__PLATFORMS__/{}/Developer/usr/lib/libXCTestBundleInject.dylib".format(
              self.test_execution_platform,
          ),
      )
    if self.sanitizer_dyld_env:
      libraries.append(self.sanitizer_dyld_env)
    if main_thread_checker_dyld_env:
      libraries.append(main_thread_checker_dyld_env)
    self.xctestrun_libraries = ":".join(libraries)

  def _configure_platform_environment(self):
    if self.test_platform_type == "macos":
      self.xctestrun_platform_environment = collections.OrderedDict([
          (
              "DYLD_FALLBACK_LIBRARY_PATH",
              "__PLATFORMS__/MacOSX.platform/Developer/usr/lib",
          ),
          (
              "DYLD_FRAMEWORK_PATH",
              (
                  "__PLATFORMS__/MacOSX.platform/Developer/Library/Frameworks:"
                  "__SHAREDFRAMEWORKS__"
              ),
          ),
          (
              "DYLD_LIBRARY_PATH",
              (
                  "__PLATFORMS__/MacOSX.platform/Developer/Library/Frameworks:"
                  "__SHAREDFRAMEWORKS__"
              ),
          ),
      ])
      return

    self.xctestrun_platform_environment = collections.OrderedDict([
        (
            "DYLD_LIBRARY_PATH",
            "__PLATFORMS__/{}/Developer/usr/lib".format(
                self.test_execution_platform,
            ),
        ),
    ])

  def _build_test_filters(self):
    tests = []
    testbridge_tests = os.environ.get("TESTBRIDGE_TEST_ONLY")
    test_filter = self.test_config["test_filter"]
    if testbridge_tests and test_filter:
      tests = "{},{}".format(testbridge_tests, test_filter).split(",")
    elif testbridge_tests:
      tests = testbridge_tests.split(",")
    elif test_filter:
      tests = test_filter.split(",")

    for test in tests:
      if not test:
        continue
      if test.startswith("-"):
        self.skip_tests.append(test[1:])
      else:
        self.only_tests.append(test)

  def _testing_identifiers(self, identifiers):
    class_methods = collections.OrderedDict()
    for identifier in identifiers:
      if not identifier:
        continue
      if "/" in identifier:
        class_name, method = identifier.split("/", 1)
      else:
        class_name = identifier
        method = ""
      class_methods.setdefault(class_name, [])
      if method:
        class_methods[class_name].append(method)

    suites = []
    xctest_classes = []
    for class_name, methods in class_methods.items():
      suite = collections.OrderedDict([("name", class_name)])
      xctest_class = collections.OrderedDict([("name", class_name)])
      if methods:
        suite["testFunctions"] = methods
        xctest_class["xctestMethods"] = methods
      suites.append(suite)
      xctest_classes.append(xctest_class)
    return collections.OrderedDict([
        ("suites", suites),
        ("xctestClasses", xctest_classes),
    ])

  def _create_simulator(self):
    self.reuse_simulator = ""
    if _bool_from_config(self.runner_config["reuse_simulator"]):
      self.reuse_simulator = "1"

    self.simulator_id = "unused"
    if self.test_platform_type == "macos":
      self.simulator_id = "macos"
      self.reuse_simulator = ""
      return

    if not self.build_for_device:
      env = os.environ.copy()
      env.update({
          "SIMULATOR_PLATFORM_TYPE": self.test_platform_type,
          "SIMULATOR_DEVICE_TYPE": self.runner_config["device_type"],
          "SIMULATOR_OS_VERSION": self.runner_config["os_version"],
          "SIMULATOR_REUSE_SIMULATOR": self.reuse_simulator,
          "SIMULATOR_SDK_VERSION": self._platform_sdk_version(),
          "XCTESTRUN_RUNNER_PID": str(os.getpid()),
      })
      self.simulator_id = _check_output(
          [self.runner_config["create_simulator_action_binary"]],
          env=env,
      )

  def _choose_execution_strategy(self):
    self.test_exit_code = 0
    self.testlog = os.path.join(self.test_tmp_dir, "test.log")
    if self.test_platform_type == "macos":
      self.intel_simulator_hack = False
      self.architecture = _check_output(["uname", "-m"])
      self.should_use_xcodebuild = True
      return

    test_file = _check_output(["file", self.test_bundle_binary])
    self.intel_simulator_hack = False
    self.architecture = "arm64"
    if _check_output(["arch"]) == "arm64" and "arm64" not in test_file:
      self.intel_simulator_hack = True
      self.architecture = "x86_64"

    self.should_use_xcodebuild = False
    if self.build_for_device:
      print("note: Using 'xcodebuild' because build for device was requested")
      self.should_use_xcodebuild = True
    if self.test_host_path:
      print("note: Using 'xcodebuild' because test host was provided")
      self.should_use_xcodebuild = True
    if self.runner_config["test_order"] == "random":
      print("note: Using 'xcodebuild' because random test order was requested")
      self.should_use_xcodebuild = True
    if self.create_xcresult_bundle:
      print("note: Using 'xcodebuild' because XCResult bundle was requested")
      self.should_use_xcodebuild = True
    if _split_command_line_args(self.command_line_args):
      print("note: Using 'xcodebuild' because '--command_line_args' was provided")
      self.should_use_xcodebuild = True
    if self.skip_tests or self.only_tests:
      print("note: Using 'xcodebuild' because test filter was provided")
      self.should_use_xcodebuild = True
    if self.custom_xcodebuild_args:
      print("note: Using 'xcodebuild' because '--xcodebuild_args' was provided")
      self.should_use_xcodebuild = True
    if self.screen_capture_format:
      print("note: Using 'xcodebuild' because a screen capture format was requested")
      self.should_use_xcodebuild = True
      self.create_xcresult_bundle = True
      if self.attachment_lifetime == "keepNever":
        self._die(
            (
                "error: 'screen_capture_format' requires 'attachment_lifetime' "
                "to be 'keepAlways' or 'deleteOnSuccess'; with 'keepNever' the "
                "capture would be discarded before it reaches the .xcresult bundle"
            ),
        )

  def _run_pre_action(self):
    env = os.environ.copy()
    env["SIMULATOR_UDID"] = self.simulator_id
    _run_check([self.runner_config["pre_action_binary"]], env=env)

  def _xctestrun_plist(self):
    product_module_name = self.test_bundle_name.replace("-", "_")
    testing_environment = collections.OrderedDict()
    testing_environment["DYLD_INSERT_LIBRARIES"] = self.xctestrun_libraries
    testing_environment.update(self.xctestrun_platform_environment)
    testing_environment["__XCODE_BUILT_PRODUCTS_DIR_PATHS"] = "/DUMMY_SRCROOT/"
    testing_environment.update(self.xctestrun_env)

    test_entry = collections.OrderedDict([
        ("ProductModuleName", product_module_name),
        ("TestBundlePath", self.xcrun_test_bundle_path),
        ("TestExecutionOrdering", self.runner_config["test_order"]),
        ("IsAppHostedTestBundle", self.xctestrun_test_host_based),
        ("TestHostPath", self.xctestrun_test_host_path),
        ("TestHostBundleIdentifier", self.xcrun_test_host_bundle_identifier),
        ("IsUITestBundle", self.xcrun_is_ui_test_bundle),
        ("IsXCTRunnerHostedTestBundle", self.xcrun_is_xctrunner_hosted_bundle),
        ("UITargetAppPath", self.xcrun_target_app_path),
        (
            "DependentProductPaths",
            [
                self.xcrun_test_bundle_path,
                self.xcrun_target_app_path,
                self.xctestrun_test_host_path,
            ],
        ),
        ("TestingEnvironmentVariables", testing_environment),
        ("SystemAttachmentLifetime", self.attachment_lifetime),
        ("UserAttachmentLifetime", self.attachment_lifetime),
        ("ClangProfileDataDirectoryPath", self.test_tmp_dir),
    ])
    if self.screen_capture_format:
      test_entry["PreferredScreenCaptureFormat"] = self.screen_capture_format
    command_line_args = _split_command_line_args(self.command_line_args)
    if command_line_args:
      test_entry["CommandLineArguments"] = command_line_args
    if self.skip_tests:
      test_entry["SkipTestIdentifiers"] = self.skip_tests
      test_entry["SkipTestingIdentifiers"] = self._testing_identifiers(
          self.skip_tests,
      )
    if self.only_tests:
      test_entry["OnlyTestIdentifiers"] = self.only_tests
      test_entry["OnlyTestingIdentifiers"] = self._testing_identifiers(
          self.only_tests,
      )

    metadata = collections.OrderedDict([
        (
            "CodeCoverageBuildableInfos",
            [
                collections.OrderedDict([
                    ("Architecture", self.architecture),
                    ("BuildableIdentifier", "000000000000000000000000:primary"),
                    ("IncludeInReport", True),
                    ("IsStatic", False),
                    ("Name", "{}.xctest".format(self.test_bundle_name)),
                    ("ProductPath", self.xcrun_test_bundle_path),
                    ("Toolchains", ["com.apple.dt.toolchain.XcodeDefault"]),
                ]),
            ],
        ),
        ("FormatVersion", 1),
    ])
    return collections.OrderedDict([
        (product_module_name, test_entry),
        ("__xctestrun_metadata__", metadata),
    ])

  def _run_with_xcodebuild(self):
    if (
        self.test_platform_type != "macos"
        and not self.test_host_path
        and self.intel_simulator_hack
    ):
      self._die(
          "error: running x86_64 tests on arm64 macs using 'xcodebuild' "
          "requires a test host",
      )

    xctestrun_file = os.path.join(self.test_tmp_dir, "tests.xctestrun")
    _write_plist(xctestrun_file, self._xctestrun_plist())

    if os.environ.get("DEBUG_XCTESTRUNNER"):
      print()
      print("xctestrun contents:")
      sys.stdout.write(_read_text_file(xctestrun_file))
      print()

    args = [
        "xcodebuild",
        "test-without-building",
        "-xctestrun",
        xctestrun_file,
        "-derivedDataPath",
        os.path.join(self.test_tmp_dir, "derived_data"),
    ]
    if self.destination_timeout:
      args.extend(["-destination-timeout", self.destination_timeout])

    if self.test_platform_type == "macos":
      args.extend([
          "-destination",
          "platform=macOS,variant=macos,arch={}".format(self.architecture),
      ])
    elif self.build_for_device:
      args.extend([
          "-destination",
          "platform={},id={}".format(
              self.xcodebuild_destination_platform,
              self.device_id,
          ),
      ])
    else:
      args.extend(["-destination", "id={}".format(self.simulator_id)])

    self.result_bundle_path = os.path.join(
        os.environ["TEST_UNDECLARED_OUTPUTS_DIR"],
        "tests.xcresult",
    )
    shutil.rmtree(self.result_bundle_path, ignore_errors=True)
    if self.create_xcresult_bundle or self.test_platform_type == "macos":
      args.extend(["-resultBundlePath", self.result_bundle_path])

    args.extend(self.custom_xcodebuild_args)
    self.test_exit_code = _stream_command_to_log(args, self.testlog)

  def _run_with_xctest(self):
    if self.test_platform_type == "macos":
      self._die("error: macOS tests require xcodebuild")

    developer_dir = _check_output(["xcode-select", "-p"])
    platform_developer_dir = os.path.join(
        developer_dir,
        "Platforms",
        self.test_execution_platform,
        "Developer",
    )
    xctest_binary = os.path.join(
        platform_developer_dir,
        "Library",
        "Xcode",
        "Agents",
        "xctest",
    )
    if self.intel_simulator_hack:
      sliced_xctest_binary = os.path.join(self.test_tmp_dir, "xctest_intel_bin")
      _run_check([
          "lipo",
          "-thin",
          "x86_64",
          "-output",
          sliced_xctest_binary,
          xctest_binary,
      ])
      xctest_binary = sliced_xctest_binary

    env = os.environ.copy()
    env.update({
        "SIMCTL_CHILD_DYLD_LIBRARY_PATH": os.path.join(
            platform_developer_dir,
            "usr",
            "lib",
        ),
        "SIMCTL_CHILD_DYLD_FALLBACK_FRAMEWORK_PATH": os.path.join(
            platform_developer_dir,
            "Library",
            "Frameworks",
        ),
        "SIMCTL_CHILD_DYLD_INSERT_LIBRARIES": self.sanitizer_dyld_env,
        "SIMCTL_CHILD_LLVM_PROFILE_FILE": self.profraw,
    })
    env.update(self.passthrough_env)
    self.test_exit_code = _stream_command_to_log(
        [
            "xcrun",
            "simctl",
            "spawn",
            self.simulator_id,
            xctest_binary,
            "-XCTest",
            "All",
            os.path.join(
                self.test_tmp_dir,
                "{}.xctest".format(self.test_bundle_name),
            ),
        ],
        self.testlog,
        env=env,
    )

  def _run_tests(self):
    if self.should_use_xcodebuild:
      self._run_with_xcodebuild()
    else:
      self._run_with_xctest()

  def _coverage_manifest(self):
    provided_coverage_manifest = self.test_config["test_coverage_manifest"]
    if _is_non_empty_file(provided_coverage_manifest):
      return provided_coverage_manifest
    return os.environ["COVERAGE_MANIFEST"]

  def _run_llvm_cov_export(self, *, export_format, args, output_path, message):
    error_file = os.path.join(self.test_tmp_dir, "llvm-cov-error.txt")
    with open(output_path, "wb") as output, open(error_file, "wb") as error:
      result = subprocess.run(
          [
              "xcrun",
              "llvm-cov",
              "export",
              "-format",
              export_format,
          ]
          + args,
          stdout=output,
          stderr=error,
          check=False,
      )

    status = result.returncode
    if _is_non_empty_file(error_file) or status:
      print("error: while exporting {}".format(message), file=sys.stderr)
      sys.stderr.write(_read_text_file(error_file))
      if status == 0:
        status = 1
    return status

  def _collect_macos_coverage(self):
    if os.environ.get("COVERAGE") != "1":
      return

    llvm_coverage_manifest = self._coverage_manifest()
    profdata = os.path.join(self.test_tmp_dir, "coverage.profdata")
    _run_check(["xcrun", "llvm-profdata", "merge", self.profraw, "--output", profdata])

    lcov_args = [
        "-instr-profile",
        profdata,
        "-ignore-filename-regex=.*external/.+",
        "-path-equivalence=.,{}".format(os.getcwd()),
        self.test_bundle_binary,
        "@{}".format(llvm_coverage_manifest),
    ]
    self.llvm_cov_status = self._run_llvm_cov_export(
        export_format="lcov",
        args=lcov_args,
        output_path=os.environ["COVERAGE_OUTPUT_FILE"],
        message="coverage report",
    )

    if os.environ.get("COVERAGE_PRODUCE_JSON"):
      self.llvm_cov_json_export_status = self._run_llvm_cov_export(
          export_format="text",
          args=lcov_args,
          output_path=os.path.join(
              os.environ["TEST_UNDECLARED_OUTPUTS_DIR"],
              "coverage.json",
          ),
          message="json coverage report",
      )

  def _collect_simulator_coverage(self):
    profdata = os.path.join(self.test_tmp_dir, self.simulator_id, "Coverage.profdata")
    if not self.should_use_xcodebuild:
      profdata = os.path.join(self.test_tmp_dir, "coverage.profdata")

    if os.environ.get("COLLECT_PROFDATA", "0") == "1" and os.path.isfile(profdata):
      _copy_file(
          profdata,
          os.path.join(
              os.environ["TEST_UNDECLARED_OUTPUTS_DIR"],
              os.path.basename(profdata),
          ),
      )

    if os.environ.get("COVERAGE") != "1" or os.environ.get("APPLE_COVERAGE") != "1":
      return

    if not self.should_use_xcodebuild:
      _run_check(["xcrun", "llvm-profdata", "merge", self.profraw, "--output", profdata])

    lcov_args = [
        "-instr-profile",
        profdata,
        "-ignore-filename-regex=.*external/.+",
        "-path-equivalence=.,{}".format(os.getcwd()),
    ]
    arch = _check_output(["uname", "-m"])
    has_binary = False
    for binary in os.environ["TEST_BINARIES_FOR_LLVM_COV"].split(";"):
      if not binary:
        continue
      if not has_binary:
        lcov_args.append(binary)
        has_binary = True
        if arch not in _check_output(["file", binary]):
          arch = "x86_64"
      else:
        lcov_args.extend(["-object", binary])
      lcov_args.append("-arch={}".format(arch))

    lcov_args.append("@{}".format(self._coverage_manifest()))
    self.llvm_cov_status = self._run_llvm_cov_export(
        export_format="lcov",
        args=lcov_args,
        output_path=os.environ["COVERAGE_OUTPUT_FILE"],
        message="coverage report",
    )

    if os.environ.get("COVERAGE_PRODUCE_JSON"):
      self.llvm_cov_json_export_status = self._run_llvm_cov_export(
          export_format="text",
          args=lcov_args,
          output_path=os.path.join(
              os.environ["TEST_UNDECLARED_OUTPUTS_DIR"],
              "coverage.json",
          ),
          message="json coverage report",
      )

  def _collect_coverage(self):
    if self.test_platform_type == "macos":
      self._collect_macos_coverage()
    else:
      self._collect_simulator_coverage()

  def _run_post_action(self):
    env = os.environ.copy()
    env.update({
        "TEST_EXIT_CODE": str(self.test_exit_code),
        "TEST_LOG_FILE": self.testlog,
        "SIMULATOR_UDID": self.simulator_id,
    })
    if self.result_bundle_path:
      env.update({
          "TEST_XCRESULT_BUNDLE_PATH": self.result_bundle_path,
          "LLVM_COV_EXIT_CODE": str(self.llvm_cov_status),
          "LLVM_COV_JSON_EXPORT_EXIT_CODE": str(self.llvm_cov_json_export_status),
      })
    result = subprocess.run(
        [self.runner_config["post_action_binary"]],
        env=env,
        check=False,
    )
    self.post_action_exit_code = result.returncode

  def _fail_for_post_action_before_cleanup(self):
    if (
        _bool_from_config(self.runner_config["post_action_determines_exit_code"])
        and self.post_action_exit_code
    ):
      self._die(
          "error: post_action exited with '{}'".format(self.post_action_exit_code),
          exit_code=self.post_action_exit_code,
      )

  def _discard_xcresult_on_success(self):
    if (
        self.test_exit_code == 0
        and self.create_xcresult_bundle
        and os.environ.get("KEEP_XCRESULT_ON_SUCCESS", "1") != "1"
    ):
      shutil.rmtree(self.result_bundle_path, ignore_errors=True)

  def _clean_up_simulator(self):
    if self.test_platform_type == "macos":
      return

    env = os.environ.copy()
    env.update({
        "SIMULATOR_UDID": self.simulator_id,
        "SIMULATOR_REUSE_SIMULATOR": self.reuse_simulator,
        "XCTESTRUN_RUNNER_PID": str(os.getpid()),
    })
    _run_check([self.runner_config["clean_up_simulator_action_binary"]], env=env)

  def _fail_for_test_or_post_action(self):
    if _bool_from_config(self.runner_config["post_action_determines_exit_code"]):
      if self.post_action_exit_code:
        self._die(
            "error: post_action exited with '{}'".format(self.post_action_exit_code),
            exit_code=self.post_action_exit_code,
        )
    elif self.test_exit_code:
      self._die(
          "error: tests exited with '{}'".format(self.test_exit_code),
          exit_code=self.test_exit_code,
      )

  def _verify_tests_ran(self):
    if os.environ.get("ERROR_ON_NO_TESTS_RAN", "1") != "1":
      return

    testlog = _read_text_file(self.testlog)
    parallel_testing_enabled = "-parallel-testing-enabled YES" in testlog
    no_tests_ran = False
    if parallel_testing_enabled:
      print("Parallel testing is enabled", file=sys.stderr)
      test_execution_count = len(
          re.findall(r"Test suite '.*' started.*", testlog),
      )
      if test_execution_count == 0:
        no_tests_ran = True
    else:
      print("Testing is serialized", file=sys.stderr)
      xctest_matches = re.findall(r"Executed [0-9]+ test.*,", testlog)
      swift_testing_matches = re.findall(r"Test run with [0-9]+ test.*", testlog)
      xctest_target_execution_count = xctest_matches[-1] if xctest_matches else ""
      swift_testing_target_execution_count = (
          swift_testing_matches[-1] if swift_testing_matches else ""
      )
      if (
          "Executed 0 tests, with 0 failures" in xctest_target_execution_count
          and not swift_testing_target_execution_count
      ):
        print("No tests ran -> no count lines found", file=sys.stderr)
        no_tests_ran = True
      if (
          "Executed 0 tests, with 0 failures" in xctest_target_execution_count
          and "Test run with 0 tests" in swift_testing_target_execution_count
      ):
        print("No tests ran -> count lines were 0", file=sys.stderr)
        no_tests_ran = True

    if no_tests_ran:
      self._die("error: no tests were executed, is the test bundle empty?")

  def _verify_no_false_negatives(self):
    testlog = _read_text_file(self.testlog)
    if re.search(
        r"(^Fatal error:)|(^.*:[0-9]+:\sFatal error:)|"
        r"(^libc\+\+abi.dylib: terminating with uncaught exception)",
        testlog,
        re.MULTILINE,
    ):
      self._die("error: log contained test false negative")

  def _verify_coverage_exports(self):
    if self.llvm_cov_status:
      self._die(
          "error: exporting coverage report failed",
          exit_code=self.llvm_cov_status,
      )

    if self.llvm_cov_json_export_status:
      self._die(
          "error: exporting json coverage report failed",
          exit_code=self.llvm_cov_json_export_status,
      )

  def _mark_test_complete(self):
    premature_exit_file = os.environ.get("TEST_PREMATURE_EXIT_FILE")
    if premature_exit_file and os.path.isfile(premature_exit_file):
      os.remove(premature_exit_file)


def _parse_config_args(argv):
  parser = argparse.ArgumentParser(add_help=False)
  parser.add_argument("--runner-config", required=True)
  parser.add_argument("--test-config", required=True)
  return parser.parse_known_args(argv)


def _load_json(path):
  with open(path, encoding="utf-8") as file_obj:
    return json.load(file_obj)


def main(argv):
  premature_exit_file = os.environ.get("TEST_PREMATURE_EXIT_FILE")
  if premature_exit_file:
    open(premature_exit_file, "a").close()

  try:
    config_args, remaining_args = _parse_config_args(argv)
    runner = _AppleXctestrunRunner(
        _load_json(config_args.runner_config),
        _load_json(config_args.test_config),
        remaining_args,
    )
    return runner.run()
  except _RunnerError as error:
    if error.message:
      print(error.message, file=sys.stderr)
    return error.exit_code


if __name__ == "__main__":
  sys.exit(main(sys.argv[1:]))
