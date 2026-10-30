"""Run the installer helper against ordinary temporary files, never devices."""

import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASH = ("C:/Program Files/Git/bin/bash.exe" if os.name == "nt" else shutil.which("bash"))

# Device discovery is simulated; actual copies and hashes use host commands.
HARNESS = r'''
abort() { echo "ABORT: $*" >&2; exit 1; }
ui_print() { echo "$*"; }
getprop() {
  case "$1" in
    ro.product.device) echo "${DEVICE:-shennong}";;
    ro.build.version.incremental) echo "${ROM:-OS3.0.310.0.WNBCNXM}";;
    ro.boot.slot_suffix) echo "${SLOT_PROP:-_a}";;
    ro.boot.vbmeta.device_state) echo "${PROPERTY_LOCK:-unlocked}";;
  esac
}
test() { if [ "$1" = -b ]; then [ "$MISSING_BLOCK" != 1 ] && builtin test -f "$BLOCK"; else builtin test "$@"; fi; }
readlink() {
  if [ "$BAD_TARGET" = 1 ] && [ "$2" = "$BLOCK" ]; then echo "$PWD/wrong"; else echo "$BLOCK"; fi
}
sed() {
  case "$*" in
    *"/proc/bootconfig") command sed -n "$2" bootconfig;;
    *) command sed "$@";;
  esac
}
blockdev() { echo "${BLOCK_SIZE:-8192}"; }
dd() {
  case "$*" in
    *"of=$BLOCK"*)
      echo WRITE >> writes
      [ "$MODE" != write_fail ] || return 1
      [ "$MODE" != short_write ] || { command dd if="$IMAGE" of="$BLOCK" bs=4096 count=1 conv=notrunc; return; }
      ;;
  esac
  command dd "$@"
}
sync() { [ "$MODE" != sync_fail ]; }
sha256sum() {
  if [ -f writes ] && [ "$1" = "$BLOCK" ] && [ "$MODE" = readback_fail ]; then return 1; fi
  command sha256sum "$@"
}
SLOT="${SELECTED_SLOT:-_a}"
BLOCK="$PWD/partition"
AKHOME="$PWD"
IMAGE="$PWD/candidate.img"
. ./shennong-flash.sh || exit 2
shennong_install_boot "$IMAGE" "$EXPECTED_SHA" 8192 "$STOCK_SHA"
'''


class InstallerTests(unittest.TestCase):
    def invoke(self, **env):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stock = b"S" * 8192
            image = b"K" * 8192
            (root / "partition").write_bytes(stock)
            (root / "candidate.img").write_bytes(image)
            shutil.copyfile(ROOT / "packaging/shennong-flash.sh", root / "shennong-flash.sh")
            environment = {**os.environ, "EXPECTED_SHA": hashlib.sha256(image).hexdigest(),
                           "STOCK_SHA": hashlib.sha256(stock).hexdigest(), **env}
            (root / "bootconfig").write_text(
                'androidboot.vbmeta.device_state = "' + env.get("LOCK_STATE", "unlocked") + '"\n'
                'androidboot.slot_suffix = "' + env.get("RAW_SLOT", "_a") + '"\n', newline="\n")
            result = subprocess.run([BASH, "-c", HARNESS], cwd=root, env=environment,
                                    capture_output=True, text=True, timeout=20)
            return result, (root / "partition").read_bytes(), (root / "writes").exists()

    def test_exact_image_written_and_verified(self):
        result, content, written = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(written)
        self.assertEqual(content, b"K" * 8192)
        self.assertIn("Verified", result.stdout)

    def test_wrong_device_rom_slot_lock_or_size_never_writes(self):
        for environment in [{"DEVICE": "other"}, {"ROM": "different"},
                            {"SELECTED_SLOT": "_b"}, {"RAW_SLOT": "bad"},
                            {"LOCK_STATE": "locked"}, {"BLOCK_SIZE": "4096"},
                            {"BAD_TARGET": "1"}, {"MISSING_BLOCK": "1"}]:
            with self.subTest(environment=environment):
                result, content, written = self.invoke(**environment)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(written)
                self.assertEqual(content, b"S" * 8192)

    def test_bootconfig_used_when_userspace_lock_property_is_spoofed(self):
        result, content, written = self.invoke(PROPERTY_LOCK="locked")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(written)
        self.assertEqual(content, b"K" * 8192)

    def test_spoofed_property_and_selected_slot_cannot_target_inactive_boot(self):
        result, content, written = self.invoke(SLOT_PROP="_b", SELECTED_SLOT="_b", RAW_SLOT="_a")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(written)
        self.assertEqual(content, b"S" * 8192)

    def test_payload_or_stock_hash_mismatch_never_writes(self):
        for key in ["EXPECTED_SHA", "STOCK_SHA"]:
            with self.subTest(key=key):
                result, content, written = self.invoke(**{key: "0" * 64})
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(written)
                self.assertEqual(content, b"S" * 8192)

    def test_write_sync_short_write_and_readback_errors_are_fatal(self):
        for mode in ["write_fail", "sync_fail", "short_write", "readback_fail"]:
            with self.subTest(mode=mode):
                result, _, written = self.invoke(MODE=mode)
                self.assertTrue(written)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("Verified", result.stdout)


if __name__ == "__main__":
    unittest.main()
