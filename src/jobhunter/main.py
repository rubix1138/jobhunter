"""CLI entry point for JobHunter."""

import argparse
import asyncio
import csv
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

from .db.engine import get_connection, init_db, run_migrations
from .utils.logging import setup_logging, get_logger

load_dotenv()


# ── Config loaders ────────────────────────────────────────────────────────────

def _load_settings(path: str = "config/settings.yaml") -> dict:
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def _load_queries(path: str = "config/search_queries.yaml") -> list[dict]:
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
            return data.get("queries", [])
    except FileNotFoundError:
        return []


def _load_profile():
    from .utils.profile_loader import load_profile
    profile_path = Path("profile/user_profile.yaml")
    if not profile_path.exists():
        print(f"ERROR: Profile not found at {profile_path}. Run `jobhunter init` first.")
        sys.exit(1)
    try:
        return load_profile(profile_path)
    except Exception as e:
        print(f"ERROR: Profile validation failed: {e}")
        sys.exit(1)


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_init(args) -> int:
    """Initialize the database and verify configuration."""
    logger = get_logger("jobhunter.init")

    db_path = os.environ.get("DB_PATH", "data/jobhunter.db")
    logger.info(f"Initializing database at {db_path}")
    conn = init_db(db_path)
    conn.close()
    print(f"Database initialized: {db_path}")

    # Verify Fernet key
    if not os.environ.get("FERNET_KEY"):
        from .crypto.vault import CredentialVault
        key = CredentialVault.generate_key()
        print(f"\nNo FERNET_KEY found. Generated a new key — add this to your .env:\n")
        print(f"FERNET_KEY={key}\n")
    else:
        print("FERNET_KEY: OK")

    # Verify Anthropic API key
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("WARNING: ANTHROPIC_API_KEY not set")
    else:
        print("ANTHROPIC_API_KEY: OK")

    # Verify profile exists
    profile_path = Path("profile/user_profile.yaml")
    if profile_path.exists():
        try:
            from .utils.profile_loader import load_profile
            profile = load_profile(profile_path)
            print(f"Profile loaded: {profile.full_name() or '(name not set)'}")
            for w in profile.warnings:
                print(f"  WARNING: {w}")
        except Exception as e:
            print(f"Profile validation error: {e}")
    else:
        print(f"WARNING: Profile not found at {profile_path}")

    print("\nInit complete.")
    return 0


def cmd_status(args) -> int:
    """Show system status: recent agent runs and DB stats."""
    db_path = os.environ.get("DB_PATH", "data/jobhunter.db")
    conn = init_db(db_path)

    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM jobs GROUP BY status"
        ).fetchall()
        print("Jobs by status:")
        if rows:
            for row in rows:
                print(f"  {row['status']}: {row['cnt']}")
        else:
            print("  (none)")

        rows = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM applications GROUP BY status"
        ).fetchall()
        print("\nApplications by status:")
        if rows:
            for row in rows:
                print(f"  {row['status']}: {row['cnt']}")
        else:
            print("  (none)")

        # Daily LLM spend with budget alert
        settings = _load_settings()
        daily_limit = settings.get("budget", {}).get("daily_limit_usd", 15.0)
        alert_pct = settings.get("budget", {}).get("alert_threshold_pct", 0.80)

        daily_cost = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_usage "
            "WHERE date(created_at) = date('now')"
        ).fetchone()[0]
        alert = ""
        if daily_cost >= daily_limit:
            alert = " [BUDGET EXCEEDED]"
        elif daily_cost >= daily_limit * alert_pct:
            alert = f" [WARNING: >{int(alert_pct*100)}% of daily budget]"
        print(f"\nToday's LLM spend: ${daily_cost:.4f} / ${daily_limit:.2f}{alert}")

        rows = conn.execute(
            "SELECT agent_name, status, started_at FROM agent_runs "
            "ORDER BY started_at DESC LIMIT 5"
        ).fetchall()
        print("\nRecent agent runs:")
        if rows:
            for row in rows:
                print(f"  [{row['status']}] {row['agent_name']} at {row['started_at']}")
        else:
            print("  (none)")
    finally:
        conn.close()

    return 0


def cmd_run(args) -> int:
    """Start the scheduler — all agents run on their configured schedules."""
    settings = _load_settings()
    profile = _load_profile()
    queries = _load_queries()
    db_path = os.environ.get("DB_PATH", "data/jobhunter.db")

    from .scheduler import JobHunterScheduler

    scheduler = JobHunterScheduler(
        settings=settings,
        profile=profile,
        queries=queries,
        db_path=db_path,
    )

    async def _main():
        try:
            await scheduler.start()
        finally:
            await scheduler.stop()

    asyncio.run(_main())
    return 0


def cmd_search_now(args) -> int:
    """Run the search agent once immediately."""
    settings = _load_settings()
    profile = _load_profile()
    queries = _load_queries()
    db_path = os.environ.get("DB_PATH", "data/jobhunter.db")

    if not queries:
        print("WARNING: No search queries found in config/search_queries.yaml")

    max_queries = getattr(args, "max_queries", None)
    if max_queries is not None and max_queries > 0:
        queries = queries[:max_queries]
        print(f"Limiting to first {len(queries)} search queries")

    max_pages = getattr(args, "max_pages", None)
    if max_pages is not None and max_pages > 0:
        settings.setdefault("global_filters", {})["max_pages_per_query"] = max_pages
        print(f"Limiting to {max_pages} result page(s) per query")

    from .scheduler import run_search_once
    asyncio.run(run_search_once(settings, profile, queries, db_path))
    return 0


_APPLY_TYPE_ALIASES = {
    "easy_apply":          "easy_apply",
    "workday":             "external_workday",
    "greenhouse":          "external_greenhouse",
    "lever":               "external_lever",
    "icims":               "external_icims",
    "taleo":               "external_taleo",
    "smartrecruiters":     "external_smartrecruiters",
    "jobvite":             "external_jobvite",
    "bamboohr":            "external_bamboohr",
    "successfactors":      "external_successfactors",
    "ashby":               "external_ashby",
    "theladders":          "external_theladders",
    "adp":                 "external_adp",
    "ukg":                 "external_ukg",
    "oracle":              "external_oracle",
    "other":               "external_other",
}


def _resolve_apply_types(raw: list[str]) -> list[str]:
    """Expand shorthand aliases and full apply_type values into a canonical list."""
    resolved = []
    for token in raw:
        for part in token.split(","):
            part = part.strip().lower()
            if not part:
                continue
            canonical = _APPLY_TYPE_ALIASES.get(part, part)
            resolved.append(canonical)
    return resolved


def cmd_apply_now(args) -> int:
    """Run the apply agent once immediately."""
    settings = _load_settings()
    profile = _load_profile()
    db_path = os.environ.get("DB_PATH", "data/jobhunter.db")
    dry_run = getattr(args, "dry_run", False)
    review_mode = getattr(args, "review_mode", False)
    reprobe_blocked_workday = getattr(args, "reprobe_blocked_workday", False)
    apply_type_filter = None

    raw_types = getattr(args, "apply_type", None) or []
    if raw_types:
        apply_type_filter = _resolve_apply_types(raw_types)
        print(f"Filtering to apply_type: {', '.join(apply_type_filter)}")

    if dry_run:
        print("Dry run mode — resumes and cover letters will be generated but not submitted.")
    elif review_mode:
        print("Review mode — the browser will fill each form completely, then pause for your approval before submitting.")
        print("  [Enter] = Submit   [s] = Skip this job   [q] = Quit all\n")
    if reprobe_blocked_workday:
        print("Re-probe mode — blocked Workday tenants will be retried in this run.")

    from .scheduler import run_apply_once
    asyncio.run(run_apply_once(
        settings, profile, db_path,
        dry_run=dry_run,
        review_mode=review_mode,
        apply_type_filter=apply_type_filter,
        reprobe_blocked_workday=reprobe_blocked_workday,
    ))
    return 0


def cmd_check_email(args) -> int:
    """Run the email agent once immediately."""
    settings = _load_settings()
    profile = _load_profile()
    db_path = os.environ.get("DB_PATH", "data/jobhunter.db")

    from .scheduler import run_email_once
    asyncio.run(run_email_once(settings, profile, db_path))
    return 0


def cmd_qa_log(args) -> int:
    """Show Q&A log for a specific application (or the most recent one)."""
    import json

    db_path = os.environ.get("DB_PATH", "data/jobhunter.db")
    conn = init_db(db_path)
    try:
        app_id = getattr(args, "app_id", None)
        if app_id:
            row = conn.execute(
                "SELECT a.id, a.questions_json, j.title, j.company "
                "FROM applications a JOIN jobs j ON a.job_id = j.id "
                "WHERE a.id = ?",
                (app_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT a.id, a.questions_json, j.title, j.company "
                "FROM applications a JOIN jobs j ON a.job_id = j.id "
                "WHERE a.questions_json IS NOT NULL AND a.questions_json != '[]' "
                "ORDER BY a.id DESC LIMIT 1"
            ).fetchone()

        if not row:
            print("No Q&A log found." + (" (app id not found)" if app_id else ""))
            return 0

        qa_data = json.loads(row["questions_json"] or "[]")
        if not qa_data:
            print(f"Application #{row['id']} — {row['title']} @ {row['company']}: no Q&A recorded.")
            return 0

        print(f"Q&A log for Application #{row['id']} — {row['title']} @ {row['company']}")
        print("=" * 70)
        for i, entry in enumerate(qa_data, 1):
            flags = []
            if entry.get("needs_review"):
                flags.append("NEEDS REVIEW")
            flag_str = f"  [{', '.join(flags)}]" if flags else ""
            print(f"\n{i}. Q: {entry['question']}")
            print(f"   A: {entry['answer']}")
            print(f"   Source: {entry.get('source', '?')}  Confidence: {entry.get('confidence', '?'):.2f}{flag_str}")
        print()
    finally:
        conn.close()

    return 0


def cmd_platform_stats(args) -> int:
    """Show breakdown of external ATS platforms found during search."""
    from .db.engine import get_connection, run_migrations
    from .agents.search_agent import _classify_external_url

    db_path = os.environ.get("DB_PATH", "data/jobhunter.db")
    conn = get_connection(db_path)
    run_migrations(conn)

    try:
        rows = conn.execute(
            "SELECT apply_type, external_url, COUNT(*) as cnt "
            "FROM jobs GROUP BY apply_type, external_url"
        ).fetchall()
    finally:
        conn.close()

    # Aggregate by platform — re-classify external_other by parsing external_url
    from collections import Counter
    platform_counts: Counter = Counter()
    for row in rows:
        apply_type, external_url, cnt = row["apply_type"], row["external_url"], row["cnt"]
        if apply_type == "external_other" and external_url:
            platform = _classify_external_url(external_url)
        else:
            platform = apply_type
        platform_counts[platform] += cnt

    total = sum(platform_counts.values())
    print(f"Apply-type / Platform Distribution ({total} total jobs)\n")
    print(f"  {'Platform':<30} {'Count':>6}  {'%':>5}")
    print("  " + "-" * 44)
    for platform, count in sorted(platform_counts.items(), key=lambda x: -x[1]):
        pct = count / total * 100 if total else 0
        print(f"  {platform:<30} {count:>6}  {pct:>4.1f}%")

    print()
    # Show which platforms have applicators built
    built = {"easy_apply", "external_workday"}
    external_platforms = {p for p in platform_counts if p.startswith("external_")}
    unbuilt = external_platforms - built
    if unbuilt:
        print("External platforms WITHOUT an applicator (build next):")
        for p in sorted(unbuilt, key=lambda x: -platform_counts[x]):
            print(f"  {p:<30} {platform_counts[p]:>6} jobs")
    return 0


def cmd_prepare_referral(args) -> int:
    """Fetch a job posting URL and generate tailored resume + cover letter PDFs."""
    settings = _load_settings()
    profile = _load_profile()

    url: str = args.url
    title: str | None = getattr(args, "title", None) or None
    company: str | None = getattr(args, "company", None) or None
    output_dir = Path(getattr(args, "output_dir", None) or "data/resumes")

    print(f"Preparing referral materials for: {url}")
    if title:
        print(f"  Title override:   {title}")
    if company:
        print(f"  Company override: {company}")
    print(f"  Output directory: {output_dir}")
    print()

    from .scheduler import run_referral_once

    resume_path, cover_path = asyncio.run(
        run_referral_once(
            settings=settings,
            profile=profile,
            url=url,
            output_dir=output_dir,
            title=title,
            company=company,
        )
    )

    print(f"Resume:       {resume_path}")
    print(f"Cover letter: {cover_path}")
    return 0


def cmd_daily_summary(args) -> int:
    """Print today's activity summary."""
    from .scheduler import build_daily_summary, print_daily_summary

    db_path = os.environ.get("DB_PATH", "data/jobhunter.db")
    conn = get_connection(db_path)
    run_migrations(conn)
    try:
        summary = build_daily_summary(conn)
    finally:
        conn.close()

    print_daily_summary(summary)
    return 0


def _fetch_review_rows(limit: int) -> list:
    """Load needs-review application rows from DB."""
    db_path = os.environ.get("DB_PATH", "data/jobhunter.db")
    conn = init_db(db_path)
    try:
        rows = conn.execute(
            """
            SELECT
                a.id AS app_id,
                a.job_id AS job_id,
                a.updated_at AS updated_at,
                a.error_message AS error_message,
                j.title AS title,
                j.company AS company,
                j.apply_type AS apply_type,
                j.job_url AS job_url,
                j.external_url AS external_url
            FROM applications a
            JOIN jobs j ON j.id = a.job_id
            WHERE a.status = 'needs_review'
            ORDER BY datetime(a.updated_at) DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return rows


def _latest_review_artifact(job_id: int) -> str:
    """Return newest failure context artifact path for this job id."""
    failures_dir = Path("data/logs/failures")
    artifacts = sorted(failures_dir.glob(f"job{job_id}_*.txt")) if failures_dir.exists() else []
    return str(artifacts[-1]) if artifacts else "(none)"


def cmd_review_queue(args) -> int:
    """Print applications that require manual review with latest artifact path."""
    limit = max(1, int(getattr(args, "limit", 20)))
    rows = _fetch_review_rows(limit)

    print(f"Manual Review Queue ({len(rows)} items)")
    if not rows:
        print("  (empty)")
        return 0

    for row in rows:
        latest_artifact = _latest_review_artifact(row["job_id"])
        print(f"\nApp #{row['app_id']} | Job #{row['job_id']} | {row['apply_type']}")
        print(f"  {row['title']} @ {row['company']}")
        print(f"  Updated: {row['updated_at']}")
        print(f"  Reason: {row['error_message'] or '(none)'}")
        print(f"  URL: {row['external_url'] or row['job_url']}")
        print(f"  Artifact: {latest_artifact}")
    print()
    return 0


def cmd_review_packet(args) -> int:
    """Write a review packet for manual-review items (markdown or CSV)."""
    limit = max(1, int(getattr(args, "limit", 50)))
    csv_mode = bool(getattr(args, "csv", False))
    raw_output = getattr(args, "output", None)
    if raw_output:
        output = Path(raw_output).expanduser()
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        ext = "csv" if csv_mode else "md"
        output = Path("data/logs") / f"review_packet_{stamp}.{ext}"
    output.parent.mkdir(parents=True, exist_ok=True)

    rows = _fetch_review_rows(limit)

    if csv_mode:
        with output.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "app_id",
                    "job_id",
                    "apply_type",
                    "title",
                    "company",
                    "updated_at",
                    "reason",
                    "url",
                    "artifact",
                ]
            )
            for row in rows:
                writer.writerow(
                    [
                        row["app_id"],
                        row["job_id"],
                        row["apply_type"],
                        row["title"],
                        row["company"],
                        row["updated_at"],
                        row["error_message"] or "",
                        row["external_url"] or row["job_url"],
                        _latest_review_artifact(row["job_id"]),
                    ]
                )
    else:
        lines = [
            f"# Manual Review Packet ({len(rows)} items)",
            "",
            f"Generated UTC: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}",
            "",
        ]
        if not rows:
            lines.append("No `needs_review` applications in queue.")
        else:
            for row in rows:
                latest_artifact = _latest_review_artifact(row["job_id"])
                lines.extend(
                    [
                        f"## App #{row['app_id']} | Job #{row['job_id']} | {row['apply_type']}",
                        "",
                        f"- Title: {row['title']}",
                        f"- Company: {row['company']}",
                        f"- Updated: {row['updated_at']}",
                        f"- Reason: {row['error_message'] or '(none)'}",
                        f"- URL: {row['external_url'] or row['job_url']}",
                        f"- Artifact: {latest_artifact}",
                        "",
                    ]
                )
        output.write_text("\n".join(lines), encoding="utf-8")

    print(f"Review packet written: {output}")
    return 0


# ── Parser ────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jobhunter",
        description="Automated job search and application system",
    )
    parser.add_argument(
        "--db", metavar="PATH", help="Path to SQLite database (overrides DB_PATH env var)"
    )
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )

    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("init", help="Initialize database and verify configuration")
    subs.add_parser("status", help="Show current system status and budget")
    subs.add_parser("run", help="Start the scheduler (all agents on schedule)")
    search_parser = subs.add_parser("search-now", help="Run search agent immediately")
    search_parser.add_argument(
        "--max-queries",
        type=int,
        dest="max_queries",
        metavar="N",
        help="Only run the first N search queries (useful for quick test runs)",
    )
    search_parser.add_argument(
        "--max-pages",
        type=int,
        dest="max_pages",
        metavar="N",
        help="Fetch at most N result pages per query (default: 2; use 1 for fastest test runs)",
    )
    apply_parser = subs.add_parser("apply-now", help="Run apply agent immediately")
    apply_parser.add_argument(
        "--apply-type",
        action="append",
        dest="apply_type",
        metavar="TYPE",
        help=(
            "Only attempt jobs of this apply_type. "
            "Accepts shorthand (workday, greenhouse, lever, icims, ashby, easy_apply, other, …) "
            "or the full value (external_workday). Repeatable or comma-separated. "
            "Example: --apply-type workday  or  --apply-type workday,greenhouse"
        ),
    )
    apply_parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Generate resumes and cover letters but do not submit applications",
    )
    apply_parser.add_argument(
        "--review",
        action="store_true",
        dest="review_mode",
        help="Fill each application form completely, then pause for approval before the final submit click",
    )
    apply_parser.add_argument(
        "--reprobe-blocked-workday",
        action="store_true",
        dest="reprobe_blocked_workday",
        help=(
            "Retry Workday tenants marked blocked in workday_tenants for this run only. "
            "Use with --apply-type workday when intentionally re-testing a tenant."
        ),
    )
    subs.add_parser("check-email", help="Run email agent immediately")
    subs.add_parser("daily-summary", help="Print today's activity summary")
    review_parser = subs.add_parser("review-queue", help="Show manual-review application queue")
    review_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        metavar="N",
        help="Max queue items to display (default: 20)",
    )
    packet_parser = subs.add_parser("review-packet", help="Export manual-review queue to markdown")
    packet_parser.add_argument(
        "--limit",
        type=int,
        default=50,
        metavar="N",
        help="Max queue items to export (default: 50)",
    )
    packet_parser.add_argument(
        "--output",
        metavar="PATH",
        default=None,
        help="Output markdown path (default: data/logs/review_packet_<timestamp>.md)",
    )
    packet_parser.add_argument(
        "--csv",
        action="store_true",
        dest="csv",
        help="Write packet as CSV instead of Markdown",
    )
    subs.add_parser("platform-stats", help="Show breakdown of external ATS platforms found during search")
    qa_parser = subs.add_parser("qa-log", help="Show Q&A log for an application")
    qa_parser.add_argument(
        "--app-id",
        type=int,
        dest="app_id",
        metavar="ID",
        help="Application ID (default: most recent application with Q&A)",
    )

    referral_parser = subs.add_parser(
        "prepare-referral",
        help="Fetch a job posting URL and generate tailored resume + cover letter PDFs",
    )
    referral_parser.add_argument(
        "--url",
        required=True,
        metavar="URL",
        help="Job posting URL (LinkedIn or any external URL)",
    )
    referral_parser.add_argument(
        "--title",
        metavar="TITLE",
        default=None,
        help="Override job title (skips LLM extraction for this field)",
    )
    referral_parser.add_argument(
        "--company",
        metavar="COMPANY",
        default=None,
        help="Override company name (skips LLM extraction for this field)",
    )
    referral_parser.add_argument(
        "--output-dir",
        dest="output_dir",
        metavar="DIR",
        default="data/resumes",
        help="Directory to write PDFs into (default: data/resumes)",
    )

    return parser


def app() -> None:
    parser = build_parser()
    args = parser.parse_args()

    setup_logging(level=args.log_level)

    if args.db:
        os.environ["DB_PATH"] = args.db

    handlers = {
        "init": cmd_init,
        "status": cmd_status,
        "run": cmd_run,
        "search-now": cmd_search_now,
        "apply-now": cmd_apply_now,
        "check-email": cmd_check_email,
        "daily-summary": cmd_daily_summary,
        "review-queue": cmd_review_queue,
        "review-packet": cmd_review_packet,
        "qa-log": cmd_qa_log,
        "platform-stats": cmd_platform_stats,
        "prepare-referral": cmd_prepare_referral,
    }

    handler = handlers.get(args.command)
    if handler is None:
        parser.print_help()
        sys.exit(1)

    sys.exit(handler(args))


if __name__ == "__main__":
    app()
