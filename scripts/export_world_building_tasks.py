import argparse
import csv
import io
import os
import shutil
import subprocess
from pathlib import Path, PureWindowsPath


DEFAULT_OUTPUT = Path("data/world_building_download_tasks.csv")
OUTPUT_HEADER = (
    "continent",
    "country",
    "city",
    "download_dir",
    "process_name_prefix",
)

TASK_QUERY = """
COPY (
    SELECT
        BTRIM(continent) AS continent,
        BTRIM(country) AS country,
        BTRIM(city) AS city,
        SPLIT_PART(BTRIM(shp_name), '_', 1) AS process_name_prefix
    FROM world_building
    WHERE continent IS NOT NULL
      AND BTRIM(continent) <> ''
      AND country IS NOT NULL
      AND BTRIM(country) <> ''
      AND city IS NOT NULL
      AND BTRIM(city) <> ''
      AND shp_name IS NOT NULL
      AND BTRIM(shp_name) <> ''
      AND SPLIT_PART(BTRIM(shp_name), '_', 1) <> ''
    GROUP BY
        BTRIM(continent),
        BTRIM(country),
        BTRIM(city),
        SPLIT_PART(BTRIM(shp_name), '_', 1)
    ORDER BY BTRIM(continent), BTRIM(country), BTRIM(city)
) TO STDOUT WITH (FORMAT CSV, HEADER TRUE, ENCODING 'UTF8')
""".strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Export the unique continent/country/city combinations in "
            "world_building as a download task CSV with a derived download directory."
        )
    )
    parser.add_argument("--dbname", default="building", help="PostgreSQL database name.")
    parser.add_argument("--user", default="postgres", help="PostgreSQL user name.")
    parser.add_argument("--host", default="127.0.0.1", help="PostgreSQL server host.")
    parser.add_argument("--port", type=int, default=5432, help="PostgreSQL server port.")
    parser.add_argument(
        "--password",
        help="Database password. Prefer setting PGPASSWORD instead of using this option.",
    )
    parser.add_argument(
        "--psql",
        help="Optional path to psql. When omitted, the executable is located automatically.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output CSV path. Default: {DEFAULT_OUTPUT.as_posix()}",
    )
    return parser.parse_args()


def find_psql(explicit_path: str | None) -> str:
    if explicit_path:
        path = Path(explicit_path).expanduser()
        if not path.is_file():
            raise SystemExit(f"psql executable does not exist: {path}")
        return str(path)

    discovered = shutil.which("psql")
    if discovered:
        return discovered

    windows_candidates = [
        Path(r"C:\Program Files\PostgreSQL\17\bin\psql.exe"),
        Path(r"C:\Program Files\PostgreSQL\16\bin\psql.exe"),
        Path(r"C:\Program Files\PostgreSQL\15\bin\psql.exe"),
    ]
    for candidate in windows_candidates:
        if candidate.is_file():
            return str(candidate)

    raise SystemExit("psql was not found. Install the PostgreSQL client or pass --psql PATH.")


def validate_task_csv(csv_text: str) -> list[tuple[str, str, str, str]]:
    reader = csv.reader(io.StringIO(csv_text))
    header = next(reader, None)
    if header != ["continent", "country", "city", "process_name_prefix"]:
        raise RuntimeError(
            "Database export produced an unexpected CSV header; "
            "expected continent,country,city,process_name_prefix."
        )

    tasks: list[tuple[str, str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    for line_number, row in enumerate(reader, start=2):
        if len(row) != 4:
            raise RuntimeError(
                f"Database export produced {len(row)} columns on line {line_number}; expected 4."
            )

        task = tuple(value.strip() for value in row)
        if not all(task):
            raise RuntimeError(f"Database export produced an empty field on line {line_number}.")
        task_key = task[:3]
        if task_key in seen:
            raise RuntimeError(
                "Database export produced multiple processing prefixes for the same task "
                f"on line {line_number}: {task_key}"
            )

        seen.add(task_key)
        tasks.append(task)

    if not tasks:
        raise RuntimeError("Database export returned no download tasks.")

    return tasks


def fetch_tasks(
    psql: str,
    *,
    dbname: str,
    user: str,
    host: str,
    port: int,
    password: str,
) -> list[tuple[str, str, str, str]]:
    environment = os.environ.copy()
    environment["PGPASSWORD"] = password
    environment["PGCLIENTENCODING"] = "UTF8"

    command = [
        psql,
        "-X",
        "-q",
        "-v",
        "ON_ERROR_STOP=1",
        "-h",
        host,
        "-p",
        str(port),
        "-U",
        user,
        "-d",
        dbname,
        "-c",
        TASK_QUERY,
    ]
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or "Unknown PostgreSQL error."
        raise RuntimeError(f"Failed to read world_building: {message}")

    return validate_task_csv(result.stdout)


def write_tasks(output_path: Path, tasks: list[tuple[str, str, str, str]]) -> None:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(f".{output_path.name}.tmp")

    try:
        with temporary_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(OUTPUT_HEADER)
            for continent, country, city, process_name_prefix in tasks:
                download_dir = str(PureWindowsPath("data", continent, country, city))
                writer.writerow(
                    (continent, country, city, download_dir, process_name_prefix)
                )
        temporary_path.replace(output_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def main() -> None:
    args = parse_args()
    password = args.password or os.environ.get("PGPASSWORD")
    if not password:
        raise SystemExit("Set PGPASSWORD or pass --password to connect to PostgreSQL.")

    psql = find_psql(args.psql)
    tasks = fetch_tasks(
        psql,
        dbname=args.dbname,
        user=args.user,
        host=args.host,
        port=args.port,
        password=password,
    )
    write_tasks(args.output, tasks)
    print(f"Exported {len(tasks)} grouped download tasks to {args.output.resolve()}")


if __name__ == "__main__":
    main()
