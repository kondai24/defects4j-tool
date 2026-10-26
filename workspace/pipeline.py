#!/usr/bin/env python3
"""Unified CLI for Defects4J workspace tasks.

Subcommands:
- checkout: checkout projects listed in a CSV and generate <target_dir>/results.csv + bug-location.md
- prompts: generate prompts next to bug Java files using <target_dir>/results.csv
- prompts_copy: copy prompts from one dir to another based on <to_dir>/results.csv
- test: run defects4j test for rows in <target_dir>/results.csv and update it
- fix: run LLM-based fix (FIX_PROVIDER=claude|codex) for rows in <target_dir>/results.csv (requires prompts next to bug files)
- run: run a multi-variant workflow (checkout/prompts/fix/test) from a YAML plan file
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, TextIO

from defects4j_tools import batch_checkout, batch_fix, batch_test, generate_prompts, prompts_copy


DEFAULT_STEPS = ["checkout", "prompts", "fix", "test"]
VALID_STEPS = {"checkout", "prompts", "test", "fix"}
VALID_PROMPTS_TYPE = {"generate", "copy"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pipeline.py", description="Defects4J workspace unified pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    p_checkout = sub.add_parser("checkout", help="Checkout projects from CSV into target dir")
    p_checkout.add_argument("--in", dest="in_csv", required=True, help="Input CSV (project,id)")
    p_checkout.add_argument("--out-dir", dest="out_dir", required=True, help="Target directory (e.g., CoT-no-AST)")

    p_prompts = sub.add_parser("prompts", help="Generate prompts using <target_dir>/results.csv")
    p_prompts.add_argument("--dir", dest="target_dir", required=True, help="Target directory (contains results.csv)")
    p_prompts.add_argument("--mode", choices=["normal", "AST", "CFG_PDG"], default="normal")

    p_prompts_copy = sub.add_parser(
        "prompts_copy",
        help="Copy prompt files from one dir to another based on <to_dir>/results.csv",
    )
    p_prompts_copy.add_argument("--from-dir", dest="from_dir", required=True, help="Source directory (already has prompts)")
    p_prompts_copy.add_argument("--to-dir", dest="to_dir", required=True, help="Destination directory (will receive prompts)")

    p_test = sub.add_parser("test", help="Run tests using <target_dir>/results.csv and update it")
    p_test.add_argument("--dir", dest="target_dir", required=True, help="Target directory (contains results.csv)")

    p_fix = sub.add_parser("fix", help="Run LLM-based fix (FIX_PROVIDER=claude|codex) using <target_dir>/results.csv")
    p_fix.add_argument("--dir", dest="target_dir", required=True, help="Target directory (contains results.csv)")
    p_fix.add_argument(
        "--model",
        dest="model",
        default=None,
        help="Model name for the provider selected by FIX_PROVIDER (required)",
    )

    p_fix.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"],
                       default=None, help="Claude reasoning effort (omitted: model default)")

    p_run = sub.add_parser("run", help="Run checkout/prompts/fix/test for multiple variants from a YAML plan")
    p_run.add_argument("--plan", dest="plan_path", required=True, help="YAML plan file path")
    p_run.add_argument(
        "--only",
        dest="only",
        default=None,
        help="Comma-separated variant names to run (default: all variants)",
    )

    return parser


def _require_dict(value: Any, where: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Expected mapping at {where}, got: {type(value).__name__}")
    return value


def _require_list(value: Any, where: str) -> List[Any]:
    if not isinstance(value, list):
        raise ValueError(f"Expected list at {where}, got: {type(value).__name__}")
    return value


def _load_yaml_plan(plan_path: Path) -> Dict[str, Any]:
    try:
        import yaml  # type: ignore
    except Exception as exc:
        raise RuntimeError(
            "YAML plan を使うには Python package 'pyyaml' が必要です。"
            "\n例: pip install pyyaml"
        ) from exc

    raw = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    return _require_dict(raw, "plan root")


def _resolve_path(base_dir: Path, value: str) -> str:
    p = Path(value).expanduser()
    if p.is_absolute():
        return str(p)
    return str((base_dir / p).resolve())


class _Tee:
    def __init__(self, primary: TextIO, secondary: TextIO):
        self._primary = primary
        self._secondary = secondary

    def write(self, s: str) -> int:
        a = self._primary.write(s)
        self._secondary.write(s)
        return a

    def flush(self) -> None:
        self._primary.flush()
        self._secondary.flush()

    def isatty(self) -> bool:  # pragma: no cover
        try:
            return bool(self._primary.isatty())
        except Exception:
            return False


def _normalize_steps(value: Any, where: str) -> List[str]:
    if value is None:
        return list(DEFAULT_STEPS)
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ValueError(f"{where} must be a list of strings")
    unknown = [s for s in value if s not in VALID_STEPS]
    if unknown:
        raise ValueError(f"Unknown steps in {where}: {unknown}. Valid: {sorted(VALID_STEPS)}")
    return value


def _variant_model(defaults: Dict[str, Any], variant: Dict[str, Any]) -> Optional[str]:
    model = str(variant.get("model") or defaults.get("model") or "").strip()
    return model or None


def _run_variant(
    *,
    plan_base_dir: Path,
    run_base_dir: Path,
    in_csv: str,
    defaults: Dict[str, Any],
    variant: Dict[str, Any],
    name_to_outdir: Dict[str, str],
) -> int:
    name = str(variant.get("name") or "").strip() or str(variant.get("out_dir") or "").strip()
    if not name:
        raise ValueError("Each variant must have 'name' or 'out_dir'")

    out_dir_raw = str(variant.get("out_dir") or "").strip()
    if not out_dir_raw:
        raise ValueError(f"variant '{name}' is missing required key: out_dir")

    variant_in_csv = str(variant.get("in_csv") or "").strip() or in_csv
    if not variant_in_csv:
        raise ValueError(f"variant '{name}' is missing in_csv (and plan root has no in_csv)")

    steps = _normalize_steps(variant.get("steps", defaults.get("steps")), f"variants[{name}].steps")
    model = _variant_model(defaults, variant)
    effort = variant.get("effort", defaults.get("effort"))
    continue_on_error = bool(variant.get("continue_on_error", defaults.get("continue_on_error", True)))

    prompts_cfg = variant.get("prompts", defaults.get("prompts", {"type": "generate", "mode": "normal"}))
    prompts_cfg = _require_dict(prompts_cfg, f"variants[{name}].prompts")
    prompts_type = str(prompts_cfg.get("type") or "generate")
    if prompts_type not in VALID_PROMPTS_TYPE:
        raise ValueError(f"variants[{name}].prompts.type must be one of {sorted(VALID_PROMPTS_TYPE)}")
    prompts_mode = str(prompts_cfg.get("mode") or "normal")
    if prompts_mode not in {"normal", "AST", "CFG_PDG"}:
        raise ValueError(f"variants[{name}].prompts.mode must be one of normal/AST/CFG_PDG")

    # For plan mode:
    # - out_dir / prompts.from_dir are resolved relative to CURRENT working directory.
    # - in_csv is resolved relative to the plan file directory.
    out_dir = _resolve_path(run_base_dir, out_dir_raw)
    resolved_in_csv = _resolve_path(plan_base_dir, variant_in_csv)

    out_dir_path = Path(out_dir)
    if not out_dir_path.exists() or not out_dir_path.is_dir():
        print(f"Error: variant '{name}': out_dir not found: {out_dir_path}", file=sys.stderr)
        return 2

    log_path = out_dir_path / "run_all_results.txt"
    with log_path.open("w", encoding="utf-8", newline="\n") as log_fh:
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = _Tee(old_out, log_fh)  # type: ignore[assignment]
        sys.stderr = _Tee(old_err, log_fh)  # type: ignore[assignment]
        try:
            print(f"[info] run_all_results.txt: {log_path}")

            print("=" * 60)
            print(f"[variant] {name}")
            print(f"  out_dir: {out_dir_raw}")
            print(f"  steps: {steps}")
            print(f"  prompts: {prompts_type}" + (f" (mode={prompts_mode})" if prompts_type == "generate" else ""))
            print(f"  provider: {batch_fix.get_provider() or '-'}")
            print(f"  model: {model or '-'}")
            print(f"  effort: {effort or 'default'}")
            print("=" * 60)

            for step in steps:
                if step == "checkout":
                    rc = batch_checkout.run(out_dir, resolved_in_csv)
                elif step == "prompts":
                    if prompts_type == "generate":
                        rc = generate_prompts.run(out_dir, prompts_mode)
                    else:
                        from_dir_raw = str(prompts_cfg.get("from_dir") or "").strip()
                        if not from_dir_raw:
                            raise ValueError(f"variants[{name}].prompts.from_dir is required when type=copy")
                        # Convenience: allow referencing another variant name.
                        from_dir_resolved = name_to_outdir.get(from_dir_raw, from_dir_raw)
                        from_dir_abs = _resolve_path(run_base_dir, from_dir_resolved)
                        from_dir_path = Path(from_dir_abs)
                        if not from_dir_path.exists() or not from_dir_path.is_dir():
                            print(f"[skip] variant '{name}': prompts.from_dir not found: {from_dir_path}")
                            return 0
                        rc = prompts_copy.run(from_dir_abs, out_dir)
                elif step == "fix":
                    rc = batch_fix.run(out_dir, model=model, effort=effort)
                elif step == "test":
                    rc = batch_test.run(out_dir)
                else:
                    raise ValueError(f"Unknown step: {step}")

                if rc != 0:
                    print(f"[warn] variant '{name}' step '{step}' failed (exit={rc})")
                    if not continue_on_error:
                        return rc
                    # Continue to next step is usually not meaningful if current step failed;
                    # stop this variant and move on.
                    break
        finally:
            sys.stdout = old_out
            sys.stderr = old_err
    return 0


def run_plan(plan_path: str, only: Optional[str]) -> int:
    plan_file = Path(plan_path).expanduser()
    if not plan_file.exists():
        print(f"Error: plan file not found: {plan_file}", file=sys.stderr)
        return 2

    plan_base_dir = plan_file.parent.resolve()
    run_base_dir = Path.cwd().resolve()
    try:
        plan = _load_yaml_plan(plan_file)
    except Exception as exc:
        print(f"Error: failed to load plan: {exc}", file=sys.stderr)
        return 2

    defaults = _require_dict(plan.get("defaults", {}), "defaults")
    in_csv = str(plan.get("in_csv") or "").strip()

    variants = _require_list(plan.get("variants", []), "variants")
    if not variants:
        print("Error: plan has no variants", file=sys.stderr)
        return 2

    wanted = None
    if only:
        wanted = {s.strip() for s in only.split(",") if s.strip()}

    # Build name->out_dir mapping for prompts.copy convenience.
    name_to_outdir: Dict[str, str] = {}
    normalized_variants: List[Dict[str, Any]] = []
    for idx, v in enumerate(variants):
        vd = _require_dict(v, f"variants[{idx}]")
        normalized_variants.append(vd)
        vname = str(vd.get("name") or "").strip()
        out_dir = str(vd.get("out_dir") or "").strip()
        if vname and out_dir:
            name_to_outdir[vname] = out_dir

    selected = [
        vd
        for vd in normalized_variants
        if wanted is None or str(vd.get("name") or vd.get("out_dir") or "").strip() in wanted
    ]

    # Check every selected output before creating directories or running any step.
    output_dirs = set()
    for vd in selected:
        name = str(vd.get("name") or vd.get("out_dir") or "").strip()
        raw = str(vd.get("out_dir") or "").strip()
        if not raw:
            print(f"Error: variant '{name}' is missing required key: out_dir", file=sys.stderr)
            return 2
        path = Path(_resolve_path(run_base_dir, raw))
        if path.exists() or path.is_symlink():
            print(f"Error: variant '{name}': out_dir already exists: {path}", file=sys.stderr)
            return 2
        if path in output_dirs:
            print(f"Error: duplicate out_dir: {path}", file=sys.stderr)
            return 2
        output_dirs.add(path)

    # fix を含む variant があれば、checkout などを始める前に provider / model を検証する
    for vd in selected:
        steps = _normalize_steps(vd.get("steps", defaults.get("steps")), "steps")
        if "fix" not in steps:
            continue
        error = batch_fix.validate_config(_variant_model(defaults, vd), vd.get("effort", defaults.get("effort")))
        if error:
            name = str(vd.get("name") or vd.get("out_dir") or "").strip()
            print(f"Error: variant '{name}': {error}", file=sys.stderr)
            return 2

    for vd in selected:
        path = Path(_resolve_path(run_base_dir, str(vd["out_dir"]).strip()))
        try:
            path.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            print(f"Error: cannot create out_dir: {path}: {exc}", file=sys.stderr)
            return 2

    overall_rc = 0
    for vd in selected:
        rc = _run_variant(
            plan_base_dir=plan_base_dir,
            run_base_dir=run_base_dir,
            in_csv=in_csv,
            defaults=defaults,
            variant=vd,
            name_to_outdir=name_to_outdir,
        )
        if rc != 0:
            overall_rc = rc
            # plan-level continue_on_error: if false, stop.
            if not bool(defaults.get("continue_on_error", True)):
                return rc
    return overall_rc


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "checkout":
        return batch_checkout.run(args.out_dir, args.in_csv)

    if args.command == "prompts":
        return generate_prompts.run(args.target_dir, args.mode)

    if args.command == "test":
        return batch_test.run(args.target_dir)

    if args.command == "prompts_copy":
        return prompts_copy.run(args.from_dir, args.to_dir)

    if args.command == "fix":
        return batch_fix.run(args.target_dir, model=args.model, effort=args.effort)

    if args.command == "run":
        return run_plan(args.plan_path, args.only)

    parser.error("Unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
