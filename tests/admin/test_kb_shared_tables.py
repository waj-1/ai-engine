import os
import psycopg2
import pytest

DSN = os.environ.get("AI_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DSN, reason="AI_DATABASE_URL not set")


def _cols(cur, table):
    cur.execute(
        "select column_name, data_type from information_schema.columns "
        "where table_schema='public' and table_name=%s order by column_name",
        (table,),
    )
    return {r[0]: r[1] for r in cur.fetchall()}


def test_ai_kb_tables_exist_with_source_scoping():
    with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
        pages = _cols(cur, "ai_kb_pages")
        emb = _cols(cur, "ai_kb_embeddings")
    assert pages, "ai_kb_pages missing — apply the migration"
    assert emb, "ai_kb_embeddings missing — apply the migration"
    for c in ("id", "source_id", "title", "type", "language", "content"):
        assert c in pages, f"ai_kb_pages.{c} missing"
    for c in ("id", "source_id", "page_id", "content", "heading", "embedding"):
        assert c in emb, f"ai_kb_embeddings.{c} missing"


def test_cascade_delete_from_data_source():
    with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "insert into public.agent_data_sources (name, kind, config, is_active) "
            "values ('pytest-kb', 'pgvector', '{}'::jsonb, true) returning source_id"
        )
        sid = cur.fetchone()[0]
        cur.execute(
            "insert into public.ai_kb_pages (source_id, title, content) "
            "values (%s, 't', 'c') returning id", (sid,))
        pid = cur.fetchone()[0]
        cur.execute(
            "insert into public.ai_kb_embeddings (source_id, page_id, content) "
            "values (%s, %s, 'c')", (sid, pid))
        conn.commit()
        cur.execute("delete from public.agent_data_sources where source_id=%s", (sid,))
        conn.commit()
        cur.execute("select count(*) from public.ai_kb_pages where source_id=%s", (sid,))
        assert cur.fetchone()[0] == 0
        cur.execute("select count(*) from public.ai_kb_embeddings where source_id=%s", (sid,))
        assert cur.fetchone()[0] == 0


def test_match_kb_embeddings_scopes_by_source():
    with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
        zero = "[" + ",".join(["0"] * 1536) + "]"
        ids = []
        for name in ("kb-a", "kb-b"):
            cur.execute("insert into public.agent_data_sources (name, kind, config, is_active) "
                        "values (%s,'pgvector','{}'::jsonb,true) returning source_id", (name,))
            sid = cur.fetchone()[0]
            cur.execute("insert into public.ai_kb_pages (source_id, title) values (%s,%s) returning id",
                        (sid, name))
            pid = cur.fetchone()[0]
            cur.execute("insert into public.ai_kb_embeddings (source_id, page_id, content, embedding) "
                        "values (%s,%s,%s,%s::vector)", (sid, pid, f"content-{name}", zero))
            ids.append(sid)
        conn.commit()
        cur.execute("select source_id, content from public.match_kb_embeddings(%s::uuid[], %s::vector, 0.0, 10, null)",
                    ([ids[0]], zero))
        rows = cur.fetchall()
        assert rows and all(str(r[0]) == str(ids[0]) for r in rows), "RPC must only return the requested source"
        cur.execute("delete from public.agent_data_sources where source_id = any(%s::uuid[])", (ids,))
        conn.commit()


def test_match_kb_embeddings_filters_by_type():
    with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
        zero = "[" + ",".join(["0"] * 1536) + "]"
        cur.execute("insert into public.agent_data_sources (name, kind, config, is_active) "
                    "values ('kb-type','pgvector','{}'::jsonb,true) returning source_id")
        sid = cur.fetchone()[0]
        cur.execute("insert into public.ai_kb_pages (source_id, title, type) "
                    "values (%s,'guide','user_guide') returning id", (sid,))
        pid_guide = cur.fetchone()[0]
        cur.execute("insert into public.ai_kb_pages (source_id, title, type) "
                    "values (%s,'faq','faq') returning id", (sid,))
        pid_faq = cur.fetchone()[0]
        cur.execute("insert into public.ai_kb_embeddings (source_id, page_id, content, embedding) "
                    "values (%s,%s,'g',%s::vector),(%s,%s,'f',%s::vector)",
                    (sid, pid_guide, zero, sid, pid_faq, zero))
        conn.commit()
        cur.execute("select page_type from public.match_kb_embeddings("
                    "%s::uuid[], %s::vector, 0.0, 10, null, 'faq')", ([sid], zero))
        rows = cur.fetchall()
        assert rows and all(r[0] == 'faq' for r in rows), "filter_type must scope by page type"
        cur.execute("delete from public.agent_data_sources where source_id=%s", (sid,))
        conn.commit()
