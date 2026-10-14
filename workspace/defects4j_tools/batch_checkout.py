#!/usr/bin/env python3
import subprocess
import sys
import os
import csv

def find_java_file_path(checkout_dir, project_name, bug_id, class_name):
    """
    クラス名からJavaファイルの相対パスを検索する
    例: org.apache.commons.cli.CommandLine -> Cli-1/src/main/java/org/apache/commons/cli/CommandLine.java
    """
    # クラス名の最後の部分をファイル名として抽出
    simple_name = class_name.split(".")[-1]
    java_filename = f"{simple_name}.java"
    
    # 複数のsrcディレクトリパターンを検索
    src_patterns = [
        "src/main/java",
        "src/java",
        "src",
        "source",
    ]
    
    for src_pattern in src_patterns:
        src_dir = os.path.join(checkout_dir, src_pattern)
        if os.path.exists(src_dir):
            # ディレクトリを再帰的に検索
            for root, dirs, files in os.walk(src_dir):
                if java_filename in files:
                    # 絶対パスを取得
                    abs_path = os.path.join(root, java_filename)
                    # チェックアウトディレクトリからの相対パスを取得
                    # 例: ./CoT-no-AST/Cli-1/src/main/java/... -> Cli-1/src/main/java/...
                    rel_path = os.path.relpath(abs_path, start=os.path.dirname(checkout_dir))
                    return rel_path
    
    return None

def extract_filename(path):
    """
    パスからファイル名を抽出
    例: Cli-1/src/java/org/apache/commons/cli/CommandLine.java -> CommandLine.java
    """
    if not path or path.startswith("ERROR:"):
        return path
    return os.path.basename(path)


def run(target_dir: str, csv_file: str) -> int:
    """Run checkout + generate <target_dir>/results.csv and <target_dir>/bug-location.md.

    Returns process-like exit code (0=success).
    """
    # (main()の実装をそのまま流用するため、ここではsys.exitしない)
    if not os.path.exists(csv_file):
        print(f"Error: {csv_file} not found", file=sys.stderr)
        return 1

    if not os.path.exists(target_dir):
        print(f"Error: target directory '{target_dir}' not found", file=sys.stderr)
        return 1
    if not os.path.isdir(target_dir):
        print(f"Error: '{target_dir}' is not a directory", file=sys.stderr)
        return 1
    if not os.access(target_dir, os.W_OK):
        print(f"Error: target directory '{target_dir}' is not writable", file=sys.stderr)
        return 1

    # 出力ファイル（target_dir直下に固定名で生成）
    output_md = os.path.join(target_dir, "bug-location.md")
    output_results_csv = os.path.join(target_dir, "results.csv")

    # 入力CSVファイルを読み込み（project,idのみ必須）
    projects: list[dict[str, str]] = []
    with open(csv_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or 'project' not in reader.fieldnames or 'id' not in reader.fieldnames:
            print(f"Error: {csv_file} must have 'project' and 'id' columns", file=sys.stderr)
            return 1
        for row in reader:
            project = (row.get('project') or '').strip()
            bug_id = (row.get('id') or '').strip()
            if not project or not bug_id:
                continue
            projects.append({'project': project, 'id': bug_id})

    # 既存results.csvがあれば読み込み（test結果を保持したいので）
    existing_results: dict[tuple[str, str], dict[str, str]] = {}
    existing_fieldnames: list[str] = []
    if os.path.exists(output_results_csv):
        try:
            with open(output_results_csv, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                existing_fieldnames = list(reader.fieldnames or [])
                for row in reader:
                    project = (row.get('project') or '').strip()
                    bug_id = (row.get('id') or '').strip()
                    if not project or not bug_id:
                        continue
                    # 計測時間など、他のステップが追加した列も保持する
                    existing_results[(project, bug_id)] = {k: (v or '') for k, v in row.items() if k}
        except Exception as exc:
            print(f"[warn] Failed to read existing results.csv: {exc}", file=sys.stderr)

    print(f"Found {len(projects)} projects to checkout")
    print(f"Target directory: {target_dir}")
    print("-" * 60)

    success_count = 0
    failed_count = 0
    failed_projects = []

    # bug-location.md用の行を作る
    md_rows: list[dict[str, str]] = []

    for i, project_data in enumerate(projects, 1):
        project_name = project_data['project']
        bug_id = project_data['id']
        print(f"\n[{i}/{len(projects)}] Processing {project_name}-{bug_id}")

        bug_version = f"{bug_id}b"
        checkout_dir = os.path.join(target_dir, f"{project_name}-{bug_id}")

        # ディレクトリが既に存在する場合は、exportのみ実行してlocationを取得
        if os.path.isdir(checkout_dir):
            print(f"⊘ Directory already exists: {project_name}-{bug_id}")
            export_cmd = ["defects4j", "export", "-p", "classes.modified"]
            try:
                result = subprocess.run(
                    export_cmd,
                    cwd=checkout_dir,
                    capture_output=True,
                    text=True,
                    check=True
                )
                class_name = ""
                lines = result.stdout.strip().split('\n')
                if lines:
                    for line in reversed(lines):
                        if line.strip():
                            class_name = line.strip()
                            print(f"  Class: {class_name}")
                            break

                location_text = ""
                if class_name:
                    java_file_path = find_java_file_path(checkout_dir, project_name, bug_id, class_name)
                    if java_file_path:
                        location_text = java_file_path
                        print(f"  Path: {java_file_path}")
                    else:
                        location_text = class_name
                        print(f"  [warn] Java file not found, using class name: {class_name}")
                else:
                    location_text = "ERROR: No class found"

                md_rows.append({'project': project_name, 'id': bug_id, 'location': location_text})
            except subprocess.CalledProcessError as e:
                print(f"  ✗ Export failed: {project_name}-{bug_id}")
                if e.stderr:
                    print(e.stderr, end='', file=sys.stderr)
                md_rows.append({'project': project_name, 'id': bug_id, 'location': "ERROR: Export failed"})
            continue

        if os.path.exists(checkout_dir):
            print(f"✗ Skipped (path exists and is not a directory): {checkout_dir}")
            failed_count += 1
            failed_projects.append(f"{project_name}-{bug_id} (invalid path)")
            md_rows.append({'project': project_name, 'id': bug_id, 'location': "ERROR: Path exists but is not a directory"})
            continue

        checkout_cmd = [
            "defects4j", "checkout",
            "-p", project_name,
            "-v", bug_version,
            "-w", checkout_dir
        ]

        try:
            subprocess.run(
                checkout_cmd,
                capture_output=True,
                text=True,
                check=True
            )
            print(f"✓ Checkout successful: {project_name}-{bug_id}")

            export_cmd = ["defects4j", "export", "-p", "classes.modified"]
            try:
                result = subprocess.run(
                    export_cmd,
                    cwd=checkout_dir,
                    capture_output=True,
                    text=True,
                    check=True
                )
                class_name = ""
                lines = result.stdout.strip().split('\n')
                if lines:
                    for line in reversed(lines):
                        if line.strip():
                            class_name = line.strip()
                            print(f"Class: {class_name}")
                            break

                location_text = ""
                if class_name:
                    java_file_path = find_java_file_path(checkout_dir, project_name, bug_id, class_name)
                    if java_file_path:
                        location_text = java_file_path
                        print(f"Path: {java_file_path}")
                    else:
                        location_text = class_name
                        print(f"[warn] Java file not found, using class name: {class_name}")
                else:
                    location_text = "ERROR: No class found"

                md_rows.append({'project': project_name, 'id': bug_id, 'location': location_text})
                success_count += 1

            except subprocess.CalledProcessError as e:
                print(f"✗ Export failed: {project_name}-{bug_id}")
                if e.stdout:
                    print(e.stdout, end='')
                if e.stderr:
                    print(e.stderr, end='', file=sys.stderr)
                md_rows.append({'project': project_name, 'id': bug_id, 'location': "ERROR: Export failed"})
                failed_count += 1
                failed_projects.append(f"{project_name}-{bug_id} (export)")

        except subprocess.CalledProcessError as e:
            print(f"✗ Checkout failed: {project_name}-{bug_id}")
            if e.stdout:
                print("STDOUT:", e.stdout, end='')
            if e.stderr:
                print("STDERR:", e.stderr, end='', file=sys.stderr)
            md_rows.append({'project': project_name, 'id': bug_id, 'location': "ERROR: Checkout failed"})
            failed_count += 1
            failed_projects.append(f"{project_name}-{bug_id} (checkout)")

    # results.csvを生成/更新（project,id,test_status,error_count,failing_tests + 既存の追加列）
    print("\n" + "-" * 60)
    print("Generating results.csv...")

    results_fieldnames = ['project', 'id', 'test_status', 'error_count', 'failing_tests']
    results_fieldnames += [c for c in existing_fieldnames if c not in results_fieldnames]
    out_rows: list[dict[str, str]] = []
    for project_data in projects:
        project = project_data['project']
        bug_id = project_data['id']
        existing = existing_results.get((project, bug_id), {})
        out_row = {col: existing.get(col, '') for col in results_fieldnames}
        out_row['project'] = project
        out_row['id'] = bug_id
        out_rows.append(out_row)

    with open(output_results_csv, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=results_fieldnames)
        writer.writeheader()
        writer.writerows(out_rows)
    print(f"✓ results.csv created/updated: {output_results_csv}")

    # Markdownファイルを生成（target_dir直下に bug-location.md）
    print("\n" + "-" * 60)
    print("Generating Markdown file...")

    md_lines: list[str] = []
    md_lines.append("| project | id | location |")
    md_lines.append("|---|---:|---|")

    md_map: dict[tuple[str, str], str] = {}
    for row in md_rows:
        md_map[(row['project'], row['id'])] = (row.get('location') or '').strip()

    for project_data in projects:
        project = project_data['project']
        bug_id = project_data['id']
        location = (md_map.get((project, bug_id), '') or '').strip()

        if location and not location.startswith("ERROR:"):
            if '/' in location or '\\' in location or '.java' in location:
                filename = extract_filename(location)
                if not location.startswith('.'):
                    location = './' + location
                location_md = f"[{filename}]({location})"
            else:
                location_md = location
        else:
            location_md = location if location else ""

        md_lines.append(f"| {project} | {bug_id} | {location_md} |")

    with open(output_md, 'w', encoding='utf-8') as f:
        f.write('\n'.join(md_lines))
        f.write('\n')
    print(f"✓ Markdown file created: {output_md}")

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Total projects: {len(projects)}")
    print(f"Successful: {success_count}")
    print(f"Failed: {failed_count}")

    if failed_projects:
        print("\nFailed projects:")
        for project in failed_projects:
            print(f"  - {project}")

    return 0

def main():
    if len(sys.argv) != 3:
        print("Usage: python -m workspace_tools.batch_checkout <target_directory> <csv_file>")
        print("Example: python -m workspace_tools.batch_checkout CoT-no-AST more-than-2-hunks.csv")
        sys.exit(1)
    
    target_dir = sys.argv[1]
    csv_file = sys.argv[2]
    
    exit_code = run(target_dir, csv_file)
    sys.exit(exit_code)

if __name__ == "__main__":
    main()
