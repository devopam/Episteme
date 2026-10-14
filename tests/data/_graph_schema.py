from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MIG = REPO / "src/episteme/data/db/migrations"


def setup_graph_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS episteme CASCADE")
        cur.execute((REPO / "src/episteme/data/db/extensions.sql").read_text(encoding="utf-8"))
        cur.execute((REPO / "src/episteme/data/db/schema.sql").read_text(encoding="utf-8"))
        for name in sorted(p.name for p in MIG.glob("000[2-6]_*.sql")):
            cur.execute((MIG / name).read_text(encoding="utf-8"))
    conn.commit()
