"""Proof of concept: score dating app reviews with Jev judgments inside DuckDB.

Loads the review CSV into a `reviews` table with a stable `id` per record, then
builds a `review_features` table (linked by `id`) using the jev community
extension, which calls TypeSafe's Jev model (https://docs.typesafe.ai):

  - content_topic / content_topic_confidence: which of a fixed set of topics
    the review discusses, plus the confidence of that choice
  - app_comparison_prob: probability the review compares its app to another app
  - author_male_prob: probability the review's author is male

After scoring, both tables are saved to a DuckDB database file (native format)
under data/, so the scored data can be reopened later without re-running
the jev calls.

Usage:
    cp .env.example .env   # then add your TypeSafe API key to it
    uv run jevdb.py --help
    uv run jevdb.py --min-word-count 20 --feature-row-limit 100

Settings are read from a .env file in the working directory (python-dotenv);
real environment variables take precedence over file values. Requires a
TypeSafe API key (https://console.typesafe.ai). Review text is sent to that
API, so keep --feature-row-limit small while experimenting; pass 0 (the
default is unlimited) once you are ready to score every eligible review.
"""

import json
import os
import sys
from pathlib import Path

import click
import duckdb
from dotenv import load_dotenv

CSV_PATH = "data/dating_app_reviews_sentiment.csv"

# Where the tables are written at the end of a run; disable with --no-save.
SAVE_PATH = "data/jevdb.duckdb"

# Default settings; override per run with --min-word-count and
# --feature-row-limit. The jev_* functions call a paid API, so cap the row
# limit while experimenting. Re-runs re-judge rows, since the answer cache
# lives only for the process.
MIN_WORD_COUNT = 20
FEATURE_ROW_LIMIT: int | None = None

TOPIC_OPTIONS = [
    "feature_request",
    "price",
    "scam",
    "dating_success",
    "app_bug",
    "match_quality",
    "support_issue",
]

TOPIC_QUESTION = (
    "Which single topic does the review in `review_text` mainly discuss? "
    "Definitions: feature_request: the reviewer asks for a new feature or a "
    "change to the app. price: cost, subscriptions, charges, or refunds. scam: "
    "accusations of scamming, fraud, fake profiles, or deceptive monetization. "
    "dating_success: the reviewer's own outcomes getting matches, conversations, "
    "or dates. app_bug: crashes, glitches, or broken functionality. "
    "match_quality: the quality or suitability of the people or matches shown. "
    "support_issue: problems with customer support, moderation, bans, or "
    "account handling."
)

COMPARISON_CONDITION = (
    "the reviewer compares the app named in `app_name` to a different app"
)

AUTHOR_MALE_CONDITION = "the author of the review in `review_text` is male"


def load_jev(conn: duckdb.DuckDBPyConnection) -> None:
    """Load the jev extension, installing it from the community repo if needed."""
    try:
        conn.execute("LOAD jev")
    except duckdb.Error:
        # The documented `FROM community_extensions` alias is not registered in
        # every build; the repository URL works everywhere.
        conn.execute(
            "INSTALL jev FROM 'https://community-extensions.duckdb.org'"
        )
        conn.execute("LOAD jev")


def build_reviews(conn: duckdb.DuckDBPyConnection) -> int:
    """Create the reviews table from the CSV, adding a stable id per record."""
    conn.execute(
        f"""
        CREATE OR REPLACE TABLE reviews AS
        SELECT row_number() OVER () AS id, *
        FROM read_csv_auto('{CSV_PATH}')
        """
    )
    return conn.execute("SELECT count(*) FROM reviews").fetchone()[0]


def build_review_features(
    conn: duckdb.DuckDBPyConnection,
    min_word_count: int,
    row_limit: int | None,
) -> int:
    """Score reviews with at least `min_word_count` words into review_features.

    Each jev_* call receives the whole row (`r`) so the model sees column names
    and values, including the review text and the app it was written about.
    """
    limit_sql = f"LIMIT {row_limit}" if row_limit is not None else ""
    conn.execute(
        f"""
        CREATE OR REPLACE TABLE review_features AS
        SELECT
            r.id,
            jev_choice(r, ?, ?) AS content_topic,
            jev_confidence(r, ?, 'choice', ?) AS content_topic_confidence,
            jev_prob(r, ?) AS app_comparison_prob,
            jev_prob(r, ?) AS author_male_prob
        FROM reviews r
        WHERE r.review_word_count >= ?
        {limit_sql}
        """,
        [
            TOPIC_QUESTION,
            TOPIC_OPTIONS,
            TOPIC_QUESTION,
            TOPIC_OPTIONS,
            COMPARISON_CONDITION,
            AUTHOR_MALE_CONDITION,
            min_word_count,
        ],
    )
    return conn.execute("SELECT count(*) FROM review_features").fetchone()[0]


def report(
    conn: duckdb.DuckDBPyConnection,
    scored: int,
    min_word_count: int,
) -> None:
    """Print feature samples, aggregates, and API usage/cost from jev_stats()."""
    eligible = conn.execute(
        "SELECT count(*) FROM reviews WHERE review_word_count >= ?",
        [min_word_count],
    ).fetchone()[0]
    print(
        f"reviews with >= {min_word_count} words: {eligible}, scored: {scored}"
    )

    print("\nSample of scored features:")
    rows = conn.execute(
        """
        SELECT r.id, r.app_name, r.review_text, f.content_topic,
               f.content_topic_confidence, f.app_comparison_prob,
               f.author_male_prob
        FROM review_features f
        JOIN reviews r USING (id)
        ORDER BY r.id
        LIMIT 8
        """
    ).fetchall()
    for rid, app, text, topic, conf, comp, male in rows:
        snippet = text if len(text) <= 60 else text[:57] + "..."
        print(f"  [{rid}] {app}: {snippet!r}")
        print(
            f"      topic={topic} conf={conf:.2f} "
            f"compare_app={comp:.2f} author_male={male:.2f}"
        )

    print("\nTopic distribution across scored rows:")
    for topic, count, avg_conf in conn.execute(
        """
        SELECT content_topic, count(*), round(avg(content_topic_confidence), 2)
        FROM review_features
        GROUP BY 1
        ORDER BY 2 DESC
        """
    ).fetchall():
        print(f"  {topic:<16} count={count:<4} avg_conf={avg_conf}")

    avg_comp, avg_male = conn.execute(
        """
        SELECT round(avg(app_comparison_prob), 3), round(avg(author_male_prob), 3)
        FROM review_features
        """
    ).fetchone()
    print(f"\naverages: compare_app={avg_comp}, author_male={avg_male}")

    stats = json.loads(conn.execute("SELECT jev_stats()").fetchone()[0])
    print(
        "\njev stats: "
        f"requests={stats['requests']} rows={stats['rows_evaluated']} "
        f"cache_hits={stats['cache_hits']} "
        f"estimated_cost=${stats['estimated_cost_usd']}"
    )


TABLES = ("reviews", "review_features")


def save_tables(conn: duckdb.DuckDBPyConnection, path: str) -> dict[str, int]:
    """Copy the tables to a DuckDB database file in native format.

    The file keeps the schema and types of the in-memory tables, so it can be
    reopened later (duckdb.connect(path)) without re-running the jev scoring.
    Each run overwrites the tables, so the file always reflects the latest
    run's row limit and word-count threshold.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    escaped_path = str(target).replace("'", "''")
    conn.execute(f"ATTACH '{escaped_path}' AS save_db")
    try:
        counts = {}
        for table in TABLES:
            conn.execute(
                f"CREATE OR REPLACE TABLE save_db.{table} AS "
                f"SELECT * FROM {table}"
            )
            counts[table] = conn.execute(
                f"SELECT count(*) FROM save_db.{table}"
            ).fetchone()[0]
        conn.execute("CHECKPOINT save_db")
    finally:
        conn.execute("DETACH save_db")
    return counts


@click.command()
@click.option(
    "--min-word-count",
    type=click.IntRange(min=1),
    default=MIN_WORD_COUNT,
    show_default=True,
    help="Only score reviews with at least this many words.",
)
@click.option(
    "--feature-row-limit",
    type=click.IntRange(min=0),
    default=FEATURE_ROW_LIMIT,
    show_default="unlimited",
    help="Cap rows scored per run, since jev calls a paid API. "
    "0 also means unlimited.",
)
@click.option(
    "--save-path",
    type=click.Path(dir_okay=False),
    default=SAVE_PATH,
    show_default=True,
    help="DuckDB file (native format) to write the tables to.",
)
@click.option(
    "--no-save",
    is_flag=True,
    default=False,
    help="Skip writing the tables to a DuckDB file.",
)
def main(
    min_word_count: int,
    feature_row_limit: int | None,
    save_path: str,
    no_save: bool,
) -> None:
    """Build the reviews table and score reviews with Jev judgments."""
    # Read .env from the working directory; real environment variables
    # take precedence over file values.
    load_dotenv()

    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        sys.exit(
            "error: TYPESAFE_API_KEY is not set "
            "(get one at https://console.typesafe.ai)"
        )

    conn = duckdb.connect()
    load_jev(conn)
    # The extension also falls back to this env var, but set it explicitly so
    # the dependency is visible here.
    escaped_key = api_key.replace("'", "''")
    conn.execute(f"SET jev_api_key = '{escaped_key}'")

    build_reviews(conn)
    # 0 and None both mean "score every eligible review".
    row_limit = feature_row_limit or None
    scored = build_review_features(conn, min_word_count, row_limit)
    report(conn, scored, min_word_count)

    if not no_save:
        counts = save_tables(conn, save_path)
        summary = ", ".join(f"{name}={rows}" for name, rows in counts.items())
        print(f"\nsaved tables to {save_path} ({summary})")


if __name__ == "__main__":
    main()
