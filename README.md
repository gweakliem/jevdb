# jevdb

Proof of concept for scoring data with AI judgments directly in DuckDB, using
the [jev community extension](https://duckdb.org/community_extensions/extensions/jev),
which calls [TypeSafe's Jev](https://docs.typesafe.ai) model.

## What it does

1. Loads `data/dating_app_reviews_sentiment.csv` into a `reviews` table,
   adding a stable `id` column to each record.
2. Builds a `review_features` table (linked to `reviews` by `id`) for reviews
   meeting the word-count threshold (default: at least 20 words), scoring
   each row with four judgments:
   - `content_topic` — one of `feature_request`, `price`, `scam`,
     `dating_success`, `app_bug`, `match_quality`, `support_issue`
   - `content_topic_confidence` — confidence of that choice
   - `app_comparison_prob` — probability the review compares its app to
     a different app
   - `author_male_prob` — probability the review's author is male
3. Prints samples, aggregates, and API usage/cost (`jev_stats()`).
4. Saves both tables to `data/jevdb.duckdb` (DuckDB native format) so the
   scored data can be reopened later without re-running the jev calls.

## Setup

```sh
uv sync
cp .env.example .env             # then add your key from console.typesafe.ai
uv run jevdb.py
```

Environment settings are loaded from `.env` via python-dotenv; real
environment variables take precedence over file values.

## Options

- `--min-word-count N` — only score reviews with at least N words (default: 20)
- `--feature-row-limit N` — cap how many rows are scored per run. The jev
  functions call a paid API, so keep this small while experimenting. Default:
  unlimited (`0` also means unlimited).

```sh
uv run jevdb.py --min-word-count 30 --feature-row-limit 100
```

- `--save-path PATH` — where to write the DuckDB file
  (default: `data/jevdb.duckdb`)
- `--no-save` — skip writing the file

## Reusing the saved data

The saved file keeps the native schema and types. Reopen it with:

```python
import duckdb

con = duckdb.connect("data/jevdb.duckdb")
con.sql("""
    SELECT r.app_name, f.content_topic, count(*)
    FROM reviews r JOIN review_features f USING (id)
    GROUP BY 1, 2
""").show()
```

Each run overwrites the saved tables, so the file reflects the latest run's
row limit and word-count threshold.

Re-runs re-judge rows, since the answer cache lives only for the process.
Review text is sent to the TypeSafe API, so only point this at data you are
willing to share.
