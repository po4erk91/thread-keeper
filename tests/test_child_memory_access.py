"""Capabilities must survive spoofed labels, direct calls and scoped retrieval."""
from __future__ import annotations

import asyncio
import inspect
import sqlite3
import time

import pytest


PROJECT = 'prj_' + 'a' * 20
OTHER = 'prj_' + 'b' * 20
CHILD = '11111111-2222-3333-4444-555555555555'


def _tool(pkg, name):
    return pkg['mcp']._tool_manager._tools[name].fn


def _grant(pkg, monkeypatch, access='read_only', namespace=PROJECT):
    from threadkeeper.memory_access import token_digest
    conn = pkg['db'].get_db()
    conn.execute(
        'INSERT INTO child_memory_grants VALUES (?,?,?,?,?,?,?)',
        (CHILD, 'tk_test', token_digest('test-token'), access, namespace, 'spawned', 1),
    )
    conn.commit()
    monkeypatch.setenv('THREADKEEPER_FORCE_CID', CHILD)
    monkeypatch.setenv('THREADKEEPER_MEMORY_TOKEN', 'test-token')
    monkeypatch.setenv('THREADKEEPER_MEMORY_ACCESS', 'write')  # spoofing is ignored
    return conn


def _call(fn, *args, **kwargs):
    value = fn(*args, **kwargs)
    return asyncio.run(value) if inspect.isawaitable(value) else value


def test_read_only_denies_every_durable_tool_family(fresh_mp, monkeypatch):
    from threadkeeper._mcp import WRITE_TOOL_NAMES
    from threadkeeper.memory_access import _OPERATIONAL_TOOLS, _OBSERVATIONAL_TOOLS

    conn = _grant(fresh_mp, monkeypatch)
    names = WRITE_TOOL_NAMES - _OPERATIONAL_TOOLS - _OBSERVATIONAL_TOOLS
    assert {'note', 'lesson_append', 'skill_manage', 'dialectic_claim',
            'verbatim_user', 'core_set', 'accept_candidate', 'forget'} <= names
    for name in sorted(names):
        # Authorization happens before argument binding or side effects.
        assert _call(_tool(fresh_mp, name)) == 'ERR memory_access_denied', name
    assert conn.execute('SELECT COUNT(*) FROM memory_proposals').fetchone()[0] == 0
    conn.close()


def test_storage_and_filesystem_writers_fail_closed(fresh_mp, monkeypatch):
    from threadkeeper.lessons import append_lesson, patch_lesson, remove_lesson, restore_lesson_section
    from threadkeeper.tools.skills import _action_create

    observer = _grant(fresh_mp, monkeypatch)
    conn = fresh_mp['db'].get_db()
    for sql in [
        "INSERT INTO style(key,value,updated_at) VALUES ('p','bad',1)",
        "UPDATE memory_authority SET authority_class='trusted'",
        "UPDATE child_memory_grants SET access='write'",
        "INSERT INTO memory_proposals(child_cid,tool_name,arguments,memory_scope,created_at) VALUES ('x','note','{}','global',1)",
    ]:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(sql)
    for fn, args in [
        (append_lesson, ('Blocked', 'bad')), (patch_lesson, ('x', 'a', 'b')),
        (remove_lesson, ('x',)), (restore_lesson_section, ('x', 'bad')),
        (_action_create, ('blocked', 'bad', 'bad')),
    ]:
        with pytest.raises(PermissionError, match='memory_access_denied'):
            fn(*args)
    conn.close()
    observer.close()


@pytest.mark.parametrize('token', ['', 'wrong-token'])
def test_child_without_valid_grant_cannot_spoof_foreground(fresh_mp, monkeypatch, token):
    from threadkeeper import config
    from threadkeeper.authority import authority_for_origin

    observer = _grant(fresh_mp, monkeypatch, 'write', '*')
    monkeypatch.setenv('THREADKEEPER_MEMORY_TOKEN', token)
    monkeypatch.setattr(config, 'SPAWNED_CHILD', True)
    assert _tool(fresh_mp, 'verbatim_user')('fake user') == 'ERR memory_access_denied'
    assert authority_for_origin('foreground') == 'observed'
    observer.close()


@pytest.mark.parametrize('accept', [False, True])
def test_proposals_need_foreground_review_and_keep_observed_origin(fresh_mp, monkeypatch, accept):
    monkeypatch.setenv('THREADKEEPER_PROJECT_NAMESPACE', PROJECT)
    observer = _grant(fresh_mp, monkeypatch, 'propose')
    result = _tool(fresh_mp, 'verbatim_user')('generated claim')
    assert result == 'ok proposal=1 status=pending'
    assert observer.execute('SELECT COUNT(*) FROM verbatim').fetchone()[0] == 0
    assert observer.execute('SELECT COUNT(*) FROM memory_authority').fetchone()[0] == 0
    assert _call(_tool(fresh_mp, 'memory_proposal_decide'), 1, True) == 'ERR memory_access_denied'
    monkeypatch.delenv('THREADKEEPER_FORCE_CID')
    monkeypatch.delenv('THREADKEEPER_MEMORY_TOKEN')
    assert 'generated claim' in _tool(fresh_mp, 'memory_proposals')()
    result = _call(_tool(fresh_mp, 'memory_proposal_decide'), 1, accept)
    assert f'status={"accepted" if accept else "rejected"}' in result
    if accept:
        stamp = observer.execute('SELECT authority_class,source_principal FROM memory_authority').fetchone()
        assert tuple(stamp) == ('observed', 'child:' + CHILD)
    assert 'pending_proposal_not_found' in _call(_tool(fresh_mp, 'memory_proposal_decide'), 1, True)
    observer.close()


def test_namespace_cap_filters_sql_fts_and_dense_candidates(fresh_mp, monkeypatch):
    import numpy as np
    from threadkeeper import embeddings, retrieval
    from threadkeeper.project_scope import recall_where

    conn = fresh_mp['db'].get_db()
    generation = embeddings.embedding_fingerprint()
    for i in range(60):
        pid = PROJECT if i == 0 else OTHER
        vector = np.array([0.8, 0.6] if i == 0 else [1.0, 0.0], dtype='float32').tobytes()
        conn.execute(
            'INSERT INTO notes(content,kind,created_at,project_id,memory_scope,embedding,embed_backend) '
            "VALUES (?,'insight',1,?,'project',?,?)",
            ('namespace marker', pid, vector, generation),
        )
        conn.execute(
            'INSERT INTO dialog_messages(uuid,source,project,session_id,role,content,created_at,project_id,memory_scope,embedding,embed_backend) '
            "VALUES (?,'pytest','p','s','user',?,1,?,'project',?,?)",
            (str(i), 'namespace marker', pid, vector, generation),
        )
    conn.commit()
    observer = _grant(fresh_mp, monkeypatch)
    # Direct helpers receive a pre-existing raw connection: the predicates
    # themselves must constrain candidates, independently of temporary views.
    assert len(retrieval._notes_fts_search(conn, 'namespace', 1)) == 1
    assert embeddings._fts_search(conn, 'namespace', 1)[0]['uuid'] == '0'
    monkeypatch.setattr(embeddings, '_vec_on', lambda: False)
    monkeypatch.setattr(embeddings, '_encode_for_generation', lambda *a: np.array([[1, 0]], dtype='float32'))
    assert embeddings._cosine_search(conn, 'namespace', 1)[0]['project_id'] == PROJECT
    assert embeddings._dialog_cosine_search(conn, 'namespace', 1)[0]['uuid'] == '0'
    scoped = fresh_mp['db'].get_db()
    assert scoped.execute('SELECT COUNT(*) FROM notes').fetchone()[0] == 1
    with pytest.raises(sqlite3.DatabaseError):
        scoped.execute('SELECT * FROM main.notes').fetchall()
    where, params = recall_where('notes', current=OTHER, cross_project=True)
    assert scoped.execute(f'SELECT COUNT(*) FROM notes WHERE {where}', params).fetchone()[0] == 1
    scoped.close()
    observer.close()
    conn.close()


def test_v5_migration_preserves_project_global_and_unresolved_scopes(fresh_mp):
    conn = fresh_mp['db'].get_db()
    conn.executemany(
        'INSERT INTO notes(content,kind,created_at,project_id,memory_scope) VALUES (?,\'insight\',1,?,?)',
        [('local', PROJECT, 'project'), ('portable', None, 'global'), ('shared', None, 'shared'), ('unknown', None, 'unresolved')],
    )
    conn.commit()
    before = [tuple(r) for r in conn.execute('SELECT content,project_id,memory_scope FROM notes')]
    conn.execute('PRAGMA user_version=5')
    conn.commit()
    fresh_mp['db'].bootstrap_db(force=True)
    after = [tuple(r) for r in conn.execute('SELECT content,project_id,memory_scope FROM notes')]
    assert before == after
    assert conn.execute('PRAGMA user_version').fetchone()[0] == 6
    conn.close()


def test_proposal_review_cannot_mutate_another_namespace(fresh_mp, monkeypatch):
    monkeypatch.setenv('THREADKEEPER_PROJECT_NAMESPACE', OTHER)
    foreign_thread = _tool(fresh_mp, 'open_thread')('private foreign thread')
    monkeypatch.setenv('THREADKEEPER_PROJECT_NAMESPACE', PROJECT)
    observer = _grant(fresh_mp, monkeypatch, 'propose')
    assert _tool(fresh_mp, 'note')(foreign_thread, 'forged update').startswith('ok proposal=')
    monkeypatch.delenv('THREADKEEPER_FORCE_CID')
    monkeypatch.delenv('THREADKEEPER_MEMORY_TOKEN')
    result = _call(_tool(fresh_mp, 'memory_proposal_decide'), 1, True)
    assert 'status=failed' in result
    assert observer.execute('SELECT COUNT(*) FROM notes').fetchone()[0] == 0
    observer.close()


def test_trusted_child_writes_stay_observed_with_foreground_label(fresh_mp, monkeypatch):
    from threadkeeper.tools import style
    from threadkeeper.authority import authorize_action

    observer = _grant(fresh_mp, monkeypatch, 'write', '*')
    monkeypatch.setattr(style, 'WRITE_ORIGIN', 'foreground')
    assert _tool(fresh_mp, 'verbatim_user')('generated text') == 'ok'
    row = observer.execute('SELECT artifact_id, authority_class FROM memory_authority').fetchone()
    assert row['authority_class'] == 'observed'
    assert not authorize_action(observer, 'verbatim', row['artifact_id']).allowed
    observer.close()


def test_reviewed_derivation_stays_observed_and_keeps_forgetting_edges(fresh_mp, monkeypatch):
    from threadkeeper.authority import invalidate_descendants

    monkeypatch.setenv('THREADKEEPER_PROJECT_NAMESPACE', PROJECT)
    claim = _tool(fresh_mp, 'dialectic_claim')('A trusted source')
    claim_id = claim.split('id=', 1)[1].split()[0]
    observer = _grant(fresh_mp, monkeypatch, 'propose')
    result = _tool(fresh_mp, 'lesson_append')(
        title='Bound generated evidence', body='Check generated evidence before applying its advice.',
        source='claim:' + claim_id,
    )
    assert result.startswith('ok proposal=')
    monkeypatch.delenv('THREADKEEPER_FORCE_CID')
    monkeypatch.delenv('THREADKEEPER_MEMORY_TOKEN')
    assert 'status=accepted' in _call(_tool(fresh_mp, 'memory_proposal_decide'), 1, True)
    row = observer.execute(
        "SELECT authority_class FROM memory_authority WHERE artifact_kind='lesson'"
    ).fetchone()
    assert row[0] == 'observed'
    assert invalidate_descendants(observer, [('claim', claim_id)]) == 2
    observer.rollback()
    observer.close()


def test_vec0_candidate_payloads_are_namespace_filtered(fresh_mp, monkeypatch):
    import numpy as np
    from threadkeeper import embeddings

    conn = fresh_mp['db'].get_db()
    if not embeddings._vec_on():
        pytest.skip('sqlite-vec extension unavailable; requires the semantic extra')
    dim = fresh_mp['config'].EMBED_DIM
    query = np.zeros(dim, dtype='float32')
    query[0] = 1
    for i, pid in enumerate([PROJECT, OTHER]):
        cursor = conn.execute(
            "INSERT INTO notes(content,kind,created_at,project_id,memory_scope) VALUES ('marker','insight',1,?,'project')",
            (pid,),
        )
        embeddings._vec_upsert_note(conn, cursor.lastrowid, query.tobytes())
        conn.execute(
            "INSERT INTO dialog_messages(uuid,source,project,session_id,role,content,created_at,project_id,memory_scope) "
            "VALUES (?,'pytest','p','s','user','marker',1,?,'project')",
            (str(i), pid),
        )
        embeddings._vec_upsert_dialog(conn, str(i), query.tobytes())
    conn.commit()
    observer = _grant(fresh_mp, monkeypatch)
    notes = embeddings._vec0_notes_search(conn, query.tobytes(), 2)
    dialogs = embeddings._vec0_dialog_search(conn, query.tobytes(), 2)
    assert len(notes) == len(dialogs) == 1
    assert notes[0]['project_id'] == dialogs[0]['project_id'] == PROJECT
    observer.close()
    conn.close()
