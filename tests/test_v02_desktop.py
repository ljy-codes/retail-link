"""Real filesystem regression for native lifecycle helpers, using disposable fixtures."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT
TEST_DIR = Path(__file__).resolve().parent
DESKTOP = DEV / "desktop"

DRIVER = r'''
import Foundation
@main struct LifecycleFixture {
    static func main() throws {
        let fm = FileManager.default
        let root = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
        try fm.createDirectory(at: root, withIntermediateDirectories: true)
        func app(_ folder: String, _ version: String, id: String = LifecycleFiles.applicationID) throws -> URL {
            let url = root.appendingPathComponent(folder).appendingPathComponent("零售通.app")
            try fm.createDirectory(at: url.appendingPathComponent("Contents"), withIntermediateDirectories: true)
            let info = ["CFBundleIdentifier": id, "CFBundleShortVersionString": version]
            let data = try PropertyListSerialization.data(fromPropertyList: info, format: .xml, options: 0)
            try data.write(to: url.appendingPathComponent("Contents/Info.plist"))
            try Data(version.utf8).write(to: url.appendingPathComponent("Contents/payload"))
            return url
        }
        var checks: [String] = []
        func check(_ value: Bool, _ label: String) throws {
            if !value { throw LifecycleFailure.message("ASSERTION: " + label) }; checks.append(label)
        }
        func refuses(_ label: String, _ work: () throws -> Void) throws {
            var refused = false
            do { try work() } catch { refused = true }
            try check(refused, label)
        }
        let source = try app("source", "0.2.1")
        let fresh = root.appendingPathComponent("中文 空格 安装")
        try fm.createDirectory(at: fresh, withIntermediateDirectories: true)
        let first = try LifecycleFiles.install(source: source, directory: fresh, expectedTarget: nil, verify: false)
        try check(first.backup == nil && LifecycleFiles.exists(first.target), "custom_path_first_install")
        try check(try LifecycleFiles.digest(source) == LifecycleFiles.digest(first.target), "copied_bundle_bytes_match")
        try refuses("running_target_refused") {
            _ = try LifecycleFiles.install(source: source, directory: fresh, expectedTarget: LifecycleFiles.digest(first.target), verify: false, isRunning: { _ in true })
        }
        let old = try app("upgrade", "0.1.9")
        let oldDigest = try LifecycleFiles.digest(old)
        let upgrade = try LifecycleFiles.install(source: source, directory: old.deletingLastPathComponent(), expectedTarget: oldDigest, verify: false)
        try check(upgrade.backup != nil && (try LifecycleFiles.digest(upgrade.backup!)) == oldDigest, "upgrade_retains_exact_previous_app")
        try check(try LifecycleFiles.digest(upgrade.target) == LifecycleFiles.digest(source), "upgrade_publishes_new_bundle")
        let currentDigest = try LifecycleFiles.digest(upgrade.target)
        try refuses("stale_preview_does_not_replace") {
            _ = try LifecycleFiles.install(source: source, directory: upgrade.target.deletingLastPathComponent(), expectedTarget: oldDigest, verify: false)
        }
        try check(try LifecycleFiles.digest(upgrade.target) == currentDigest, "stale_preview_preserves_target")
        let wrong = try app("unrelated", "1", id: "another.app")
        let wrongDigest = try LifecycleFiles.digest(wrong)
        try refuses("unrelated_app_not_replaced") {
            _ = try LifecycleFiles.install(source: source, directory: wrong.deletingLastPathComponent(), expectedTarget: wrongDigest, verify: false)
        }
        try refuses("unsigned_fixture_cannot_install_as_signed") {
            _ = try LifecycleFiles.install(source: source, directory: upgrade.target.deletingLastPathComponent(), expectedTarget: currentDigest)
        }
        try check(try LifecycleFiles.digest(upgrade.target) == currentDigest, "signature_failure_preserves_old_app")
        let alias = root.appendingPathComponent("alias")
        try fm.createSymbolicLink(at: alias, withDestinationURL: fresh)
        try refuses("symbolic_install_directory_refused") {
            _ = try LifecycleFiles.install(source: source, directory: alias, expectedTarget: nil, verify: false)
        }
        let support = root.appendingPathComponent("support")
        let library = root.appendingPathComponent("library")
        for name in ["history", "logs", "导入文件", "原始资料", "生成文件", "exports", "unknown-user-files"] {
            try fm.createDirectory(at: support.appendingPathComponent(name), withIntermediateDirectories: true)
        }
        let scopedFiles = ["operations/state.json", "writeback/settings.json", "writeback/templates.json"]
        let protectedFiles = ["operations/unknown-work.xlsx", "writeback/backups/original.xlsx", "writeback/operations/recovery.json"]
        for name in scopedFiles + protectedFiles {
            let file = support.appendingPathComponent(name)
            try fm.createDirectory(at: file.deletingLastPathComponent(), withIntermediateDirectories: true)
            try Data(name.utf8).write(to: file)
        }
        let ordinary = try LifecycleFiles.cleanupPlan(bundle: first.target, support: support, library: library, complete: false)
        try check(ordinary == [first.target], "ordinary_uninstall_app_only")
        let complete = try LifecycleFiles.cleanupPlan(bundle: first.target, support: support, library: library, complete: true)
        let names = Set(complete.map { $0.lastPathComponent })
        try check(names == Set(["零售通.app", "history", "logs", "state.json", "settings.json", "templates.json"]), "complete_uninstall_allowlist_preserves_sources_outputs_backups_unknown")
        let selectedPaths = Set(complete.map { $0.path })
        try check(scopedFiles.allSatisfy { selectedPaths.contains(support.appendingPathComponent($0).path) }, "complete_uninstall_selects_exact_ledger_and_writeback_settings")
        try check(!selectedPaths.contains(support.appendingPathComponent("operations").path) && !selectedPaths.contains(support.appendingPathComponent("writeback").path), "complete_uninstall_never_selects_ledger_or_writeback_parent_directory")
        try check(protectedFiles.allSatisfy { !selectedPaths.contains(support.appendingPathComponent($0).path) && LifecycleFiles.exists(support.appendingPathComponent($0)) }, "writeback_original_backups_recovery_metadata_and_unknown_ledger_files_preserved")
        try check(LifecycleFiles.exists(support.appendingPathComponent("导入文件")), "planning_is_read_only")
        let staged = root.appendingPathComponent("download.part")
        try Data("sample-经营报表".utf8).write(to: staged)
        let download = root.appendingPathComponent("下载结果.xlsx")
        try LifecycleFiles.publishDownload(source: staged, destination: download, allowRename: false)
        try check(try Data(contentsOf: download) == Data(contentsOf: staged), "same_directory_staged_copy_before_publication")
        try refuses("download_never_overwrites_existing") { try LifecycleFiles.publishDownload(source: staged, destination: download, allowRename: false) }
        let dangling = root.appendingPathComponent("dangling.xlsx")
        try fm.createSymbolicLink(atPath: dangling.path, withDestinationPath: root.appendingPathComponent("missing").path)
        try refuses("download_dangling_link_is_occupied") { try LifecycleFiles.publishDownload(source: staged, destination: dangling, allowRename: false) }
        let broken = root.appendingPathComponent("broken.xlsx")
        try refuses("failed_copy_returns_error") { try LifecycleFiles.publishDownload(source: support, destination: broken, allowRename: false) }
        try check(!LifecycleFiles.exists(broken), "failed_copy_removes_only_its_partial_file")
        let raced = root.appendingPathComponent("raced.xlsx")
        try refuses("target_created_during_copy_is_never_replaced") {
            try LifecycleFiles.publishDownload(source: staged, destination: raced, allowRename: false, beforePublish: {
                try check(!LifecycleFiles.exists(raced), "final_target_not_visible_before_complete_publication")
                try Data("external-writer".utf8).write(to: raced)
            })
        }
        try check(try String(contentsOf: raced, encoding: .utf8) == "external-writer", "racing_writer_bytes_preserved")
        let remains = try fm.contentsOfDirectory(atPath: root.path).filter { $0.hasPrefix(".retail-link-publish-") }
        try check(remains.isEmpty, "publication_race_and_failure_clean_private_stages")
        let renamed = root.appendingPathComponent("atomic.xlsx")
        try LifecycleFiles.publishDownload(source: staged, destination: renamed)
        try check(LifecycleFiles.exists(renamed) && LifecycleFiles.exists(staged), "exclusive_publication_keeps_source_caller_owned")
        let finalStages = try fm.contentsOfDirectory(atPath: root.path).filter { $0.hasPrefix(".retail-link-publish-") }
        try check(finalStages.isEmpty, "successful_publication_has_no_private_stage_left")
        let data = try JSONSerialization.data(withJSONObject: ["checks": checks, "count": checks.count], options: [.sortedKeys])
        print(String(decoding: data, as: UTF8.self))
    }
}
'''


class NativeLifecycleRegression(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("swiftc"), "requires macOS Swift compiler")
    def test_real_filesystem_contracts(self):
        with tempfile.TemporaryDirectory(prefix="retail-v02-lifecycle-") as folder:
            temp = Path(folder).resolve()
            driver = temp / "Driver.swift"
            driver.write_text(DRIVER)
            binary = temp / "fixtures"
            compiled = subprocess.run(["swiftc", str(driver), str(DESKTOP / "macos/LifecycleSupport.swift"), "-o", str(binary)],
                                      capture_output=True, text=True)
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            result = subprocess.run([str(binary), str(temp / "fixtures-data")], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(result.stdout)
            self.assertGreaterEqual(report["count"], 28)
            self.assertIn("complete_uninstall_allowlist_preserves_sources_outputs_backups_unknown", report["checks"])

    def test_resource_sampling_is_explicit_and_has_no_invented_threshold(self):
        spec = importlib.util.spec_from_file_location("desktop_measure", DESKTOP / "measure_resources.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        rows = {1: {"pid": 1, "ppid": 0}, 2: {"pid": 2, "ppid": 1},
                3: {"pid": 3, "ppid": 2}, 9: {"pid": 9, "ppid": 0}}
        self.assertEqual([item["pid"] for item in module.select_tree(rows, [1])], [1, 2, 3])
        self.assertEqual([item["pid"] for item in module.select_tree(rows, [1, 9])], [1, 2, 3, 9])
        self.assertAlmostEqual(module.cpu_seconds("1-01:02:03.50"), 90123.5)


if __name__ == "__main__":
    unittest.main()
