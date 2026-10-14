#!/usr/bin/env python3
import subprocess
import sys
import os
import csv
import re

def run_defects4j_test(project, bug_id, target_dir):
    """
    指定されたプロジェクトでdefects4j testを実行し、結果を返す
    
    Returns:
        tuple: (test_status, error_count, failing_tests)
            test_status: "success", "error", "compile error", "not found"
            error_count: 失敗テスト数（successは"0"、compile errorは"N/A"）
            failing_tests: 失敗テスト一覧（複数行）。success/compile errorは"-"。
    """
    project_dir = os.path.join(target_dir, f"{project}-{bug_id}")
    
    # ディレクトリの存在確認
    if not os.path.isdir(project_dir):
        return "not found", "", ""
    
    # defects4j testコマンドを実行
    cmd = ["defects4j", "test"]
    
    try:
        result = subprocess.run(
            cmd,
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=False
        )
        
        output = result.stdout + result.stderr
        
        # コンパイルエラーの検出
        if "Cannot compile test suite!" in output or "BUILD FAILED" in output:
            return "compile error", "N/A", "-"
        
        # Failing testsの行を検索
        failing_match = re.search(r'Failing tests:\s*(\d+)', output)
        if failing_match:
            count = int(failing_match.group(1))
            if count == 0:
                return "success", "0", "-"
            else:
                # 失敗したテストクラスを抽出してコンソールに表示
                test_lines = re.findall(r'^\s*-\s+(.+)$', output, re.MULTILINE)
                if test_lines:
                    print(f"  Failed tests:")
                    for test_line in test_lines:
                        print(f"    - {test_line}")
                failing_tests = "\n".join([f"- {t}" for t in test_lines]) if test_lines else "(unknown)"
                return "error", str(count), failing_tests
        
        # Failing testsが見つからない場合
        return "error", "Unknown", "(unknown)"
        
    except Exception as e:
        return "error", f"Exception: {str(e)}", "(unknown)"


def run(target_dir: str) -> int:
    """Run defects4j test for all rows in <target_dir>/results.csv and update it."""
    csv_file = os.path.join(target_dir, "results.csv")

    # ターゲットディレクトリの存在確認
    if not os.path.exists(target_dir):
        print(f"Error: target directory '{target_dir}' not found", file=sys.stderr)
        return 1
    if not os.path.isdir(target_dir):
        print(f"Error: '{target_dir}' is not a directory", file=sys.stderr)
        return 1

    # CSVファイルの存在確認
    if not os.path.exists(csv_file):
        print(f"Error: {csv_file} not found", file=sys.stderr)
        return 1

    # CSVファイルを読み込み
    projects = []
    fieldnames = []
    with open(csv_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames)

        # test_statusとerror_countカラムがない場合は追加
        if 'test_status' not in fieldnames:
            fieldnames.append('test_status')
        if 'error_count' not in fieldnames:
            fieldnames.append('error_count')
        if 'failing_tests' not in fieldnames:
            fieldnames.append('failing_tests')

        for row in reader:
            # 計測時間など、他のステップが追加した列も保持する
            project_data = {k: v for k, v in row.items() if k is not None}
            for col in ('test_status', 'error_count', 'failing_tests'):
                project_data.setdefault(col, '')
            projects.append(project_data)

    print(f"Found {len(projects)} projects to test")
    print(f"Target directory: {target_dir}")
    print("-" * 60)

    success_count = 0
    error_count = 0
    compile_error_count = 0
    not_found_count = 0
    failed_projects = []

    for i, project_data in enumerate(projects, 1):
        project_name = project_data['project']
        bug_id = project_data['id']
        print(f"\n[{i}/{len(projects)}] Testing {project_name}-{bug_id}")

        test_status, err_count, failing_tests = run_defects4j_test(project_name, bug_id, target_dir)

        project_data['test_status'] = test_status
        project_data['error_count'] = err_count
        if test_status in {"success", "compile error", "not found"}:
            project_data['failing_tests'] = "-"
        elif test_status == "error":
            project_data['failing_tests'] = failing_tests
        else:
            project_data['failing_tests'] = project_data.get('failing_tests', '')

        if test_status == "not found":
            print(f"✗ Directory not found: {project_name}-{bug_id}")
            not_found_count += 1
            failed_projects.append(f"{project_name}-{bug_id} (not found)")
        elif test_status == "success":
            print(f"✓ Test successful: {project_name}-{bug_id}")
            success_count += 1
        elif test_status == "compile error":
            print(f"✗ Compile error: {project_name}-{bug_id}")
            compile_error_count += 1
            failed_projects.append(f"{project_name}-{bug_id} (compile error)")
        elif test_status == "error":
            print(f"✗ Test failed: {project_name}-{bug_id} ({err_count} test(s) failed)")
            error_count += 1
            failed_projects.append(f"{project_name}-{bug_id} ({err_count} tests)")

    print("\n" + "-" * 60)
    print("Updating CSV file...")
    with open(csv_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(projects)
    print(f"✓ CSV file updated: {csv_file}")

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Total projects: {len(projects)}")
    print(f"Successful: {success_count}")
    print(f"Test failed: {error_count}")
    print(f"Compile error: {compile_error_count}")
    print(f"Not found: {not_found_count}")

    if failed_projects:
        print("\nFailed/Not found projects:")
        for project in failed_projects:
            print(f"  - {project}")

    return 0

def main():
    if len(sys.argv) != 2:
        print("Usage: python -m workspace_tools.batch_test <target_directory>")
        print("Example: python -m workspace_tools.batch_test CoT-no-AST")
        sys.exit(1)
    
    target_dir = sys.argv[1]
    sys.exit(run(target_dir))

if __name__ == "__main__":
    main()
