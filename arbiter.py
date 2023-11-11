#!/usr/bin/env python
# -*- coding: utf-8 -*-

# MIT License
#
# Copyright (c) 2023 Ernst-Georg Schmid
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import psycopg2
import psycopg2.extensions
import time
import uvicorn
import configparser
import threading
import logging
import select
from fastapi import FastAPI, Request, Response, HTTPException, Depends, Security
from pydantic import BaseModel
from typing import Literal, List, Optional
from json import dumps
from contextlib import closing
from re import search
from fastapi.security.api_key import APIKey, APIKeyHeader

__author__ = 'Ernst-Georg Schmid'
__copyright__ = 'Copyright 2023, TRAKTOR'
__credits__ = ['Ernst-Georg Schmid', 'Aimless']
__license__ = 'MIT'
__version__ = '1.5.3'
__maintainer__ = 'Ernst-Georg Schmid'
__email__ = 'pgchem@tuschehund.de'
__status__ = 'EXPERIMENTAL'


tags_metadata = [
    {
        "name": "status",
        "description": "Node status.",
    },
    {
        "name": "init_node",
        "description": "Init Node.",
    },
    {
        "name": "history",
        "description": "Auto resolution history.",
    },
    {
        "name": "drop_node",
        "description": "Drop Node.",
    },
    {
        "name": "replicaset_status",
        "description": "Replicaset status.",
    },
    {
        "name": "replication_control",
        "description": "Replication control.",
    },
    {
        "name": "add_subscription",
        "description": "Add Subscription.",
    },
    {
        "name": "remove_subscription",
        "description": "Remove Subscription.",
    },
    {
        "name": "replicaset_commit",
        "description": "COMMIT the replicaset.",
    },
    {
        "name": "replicaset_add_table",
        "description": "Add table to replicaset.",
    },
    {
        "name": "replicaset_remove_table",
        "description": "Remove table from replicaset.",
    },
]

config = configparser.ConfigParser()
config.read('arbiter.ini')

LISTEN_TIMEOUT = 60
CHECK_INTERVAL = config['DEFAULT'].getint('CheckInterval', 10)
NODE = config['DEFAULT'].getint('NodeID')
CONN_STR = config['DEFAULT']['ConnectionString']
API_ADDRESS = config['DEFAULT']['APIAddress']
API_HOST, API_PORT = API_ADDRESS.split(':')
API_KEY = config['DEFAULT']['APIKey']
LSN_RESOLVER = config['DEFAULT'].get('LSNResolver', 'none').lower()
PRE_16_COMPATIBILITY = config['DEFAULT'].getboolean(
    'Pre16Compatibility', False)
SSL_KEYFILE = config['DEFAULT'].get('SSLKeyfile')
SSL_CERTFILE = config['DEFAULT'].get('SSLCertfile')

COMMON_PATH_V1 = "/v1/arbiter"

# setup loggers
logging.config.fileConfig('logging.conf', disable_existing_loggers=False)
logger = logging.getLogger(__name__)

api_key_header = APIKeyHeader(name="X-API-KEY", auto_error=False)

SCHEMA = """CREATE SCHEMA IF NOT EXISTS trktr;"""

TABLES = """CREATE TABLE IF NOT EXISTS trktr.history (
"subscription" text not null,
 occurred timestamptz NOT NULL,
 reason text not null,
 "lsn" pg_lsn not NULL,
 "relation" text not null,
 sql_state_code text null,
 resolved timestamptz null,
 CONSTRAINT trktr_history_pkey primary key (lsn)
 );
 CREATE TABLE IF NOT EXISTS trktr.replicaset (
	table_schema text NOT NULL,
	table_name text NOT NULL,
	CONSTRAINT replicaset_pk PRIMARY KEY (table_schema, table_name)
);
"""

PUBLICATIONS = """DO $$ BEGIN IF NOT EXISTS (SELECT true FROM pg_publication WHERE pubname = 'trktr_pub_multimaster') THEN CREATE PUBLICATION trktr_pub_multimaster; END IF; END $$;"""

VIEWS = """CREATE OR REPLACE VIEW trktr.v_status
 AS SELECT {} as node_id,
 not exists ((SELECT true FROM pg_subscription WHERE not subenabled AND subname like 'trktr_sub_{}_%' limit 1)) as replicating,
 exists ((SELECT true FROM trktr.history limit 1)) as tainted,
 (select count(*) from trktr.history where resolved is not null) as auto_resolved,
(select avg(1000 * extract(epoch from (last_msg_receipt_time - last_msg_send_time))) from pg_stat_subscription where subname like 'trktr_sub_{}_%') as avg_replication_lag,
(select '{}'::boolean) as pre_16_compatibility,
(select current_setting('server_version')::float) as server_version;
create or replace view trktr.v_replicaset as
select schemaname as schema_name, tablename as table_name, 'active' as table_status from pg_catalog.pg_publication_tables t
union
select table_schema, table_name, 'pending add' from
(SELECT table_schema, table_name FROM trktr.replicaset r except
select schemaname, tablename from pg_catalog.pg_publication_tables) as t
union
select schemaname, tablename, 'pending remove' from
(select schemaname, tablename from pg_catalog.pg_publication_tables except
SELECT table_schema, table_name FROM trktr.replicaset r) as t;""".format(NODE, NODE, NODE, PRE_16_COMPATIBILITY)

FUNCTIONS = """
do $$
begin
if (current_setting('server_version')::float < 16.0 OR (select pre_16_compatibility from trktr.v_status)) then
	CREATE OR REPLACE FUNCTION trktr.tf_break_cycle()
 RETURNS trigger
 LANGUAGE plpgsql
 STRICT
AS $function$
declare
begin
  if pg_backend_pid() = ANY((select pid from pg_stat_activity where backend_type = 'logical replication worker')) then
   if not NEW.is_local then
      return null;
    else
      NEW.is_local = false;
    end if;
  end if;
  return NEW;
end;
$function$;
end if;
end $$;
create or replace procedure trktr.trktr_add_table_to_replica(table_schema text, table_name text)
language 'plpgsql'
as $$
declare
	tbl text;
	fqt text;
	trg text;
begin
		tbl := quote_ident(table_name);
		trg := 't_trktr_' || tbl;
		fqt := quote_ident(table_schema) || '.' || tbl;
        if (current_setting('server_version')::float < 16.0 OR (select pre_16_compatibility from trktr.v_status))  then
		    execute 'alter table ' || fqt || ' add column if not exists origin int2 not null default {}, add column if not exists is_local boolean not null default true';
		    execute 'create trigger ' || trg  || ' before insert or update on ' || fqt || ' for each row execute function trktr.tf_break_cycle()';
            execute 'alter table ' || fqt || ' enable always trigger ' || trg;
        end if;
        execute 'alter publication trktr_pub_multimaster add table ' || fqt;
end;
$$;
create or replace procedure trktr.trktr_remove_table_from_replica(table_schema text, table_name text)
language 'plpgsql'
as $$
declare
	tbl text;
	fqt text;
	trg text;
begin
		tbl := quote_ident(table_name);
		trg := 't_trktr_' || tbl;
		fqt := quote_ident(table_schema) || '.' || tbl;
		execute 'alter publication trktr_pub_multimaster drop table ' || fqt;
        if (current_setting('server_version')::float < 16.0 OR (select pre_16_compatibility from trktr.v_status)) then
	        execute 'drop trigger ' || trg || ' on ' || fqt;
		    execute 'alter table ' || fqt || ' drop column if exists origin, drop column if exists is_local';
        end if;
end;
$$;
create or replace procedure trktr.trktr_add_table_to_replicaset(table_schema text, table_name text)
language 'plpgsql'
as $$
declare
	tbl text;
	sh text;
begin
	tbl = quote_ident(table_name);
	sh = quote_ident(table_schema);
	if not exists((select true FROM information_schema.tables t where t.table_name = tbl and t.table_schema = sh and t.table_type = 'BASE TABLE' and t.table_catalog = current_database())) then
		raise exception 'Table %.% does not exist in database %', sh, tbl, current_database();
	end if;
	insert into trktr.replicaset (table_schema, table_name) values (sh, tbl) on conflict do nothing;
end;
$$;
create or replace procedure trktr.trktr_remove_table_from_replicaset(table_schema text, table_name text)
language 'plpgsql'
as $$
declare
	tbl text;
	sh text;
begin
	tbl = quote_ident(table_name);
	sh = quote_ident(table_schema);
	if not exists((select true FROM pg_catalog.pg_publication_tables where tablename = tbl and schemaname = sh)) then
		raise exception 'Table %.% is not published', sh, tbl;
	end if;
	delete from trktr.replicaset rs where rs.table_schema = sh and rs.table_name = tbl;
end;
$$;
create or replace procedure trktr.trktr_commit_replicaset()
language 'plpgsql'
as $$
declare
	r record;
begin
for r in (
select
	schema_name,
	table_name
from
	trktr.v_replicaset
where
	table_status = 'pending add')  --add
	 loop
		call trktr.trktr_add_table_to_replica(r.schema_name, r.table_name);
		end loop;
for r in (select
	schema_name,
	table_name
from
	trktr.v_replicaset
where
	table_status = 'pending remove') --remove
	 loop
		call trktr.trktr_remove_table_from_replica(r.schema_name, r.table_name);
		end loop;
 perform pg_notify('trktr_event', 'trktr_evt_pubchanged');
end;
$$;
CREATE OR REPLACE FUNCTION trktr.trktr_find_unresolved_conflicts(fdw text DEFAULT 'file_fdw'::text)
 RETURNS TABLE(log_time timestamp with time zone, sql_state_code text, context text, detail text)
 LANGUAGE plpgsql
 STRICT
AS $$
declare
r record;
fn text;
enc text := 'utf8';
begin
	if version() ilike any (array['%Windows%', '%mingw%', '%Visual%']) then
		enc := 'latin1';
	end if;
        if fdw = 'log_fdw' then
                create extension if not exists log_fdw;
                CREATE SERVER if not exists trktr_log_server FOREIGN DATA WRAPPER log_fdw;
                for r in SELECT file_name FROM list_postgres_log_files() where file_name LIKE 'postgresql%csv' ORDER BY 1 desc limit 1 loop
                        SELECT create_foreign_table_for_log_file('trktr.pg_log', 'trktr_log_server', r.file_name);
                end loop;
        elseif fdw = 'file_fdw' then
                create extension if not exists file_fdw;
                CREATE SERVER if not exists trktr_log_server FOREIGN DATA WRAPPER file_fdw;
                for r in SELECT "name" as file_name FROM pg_ls_logdir() where "name" LIKE 'postgresql%.csv' ORDER BY 1 desc limit 1 loop
                        fn := current_setting('log_directory')  || '/' || r.file_name;
                        execute 'CREATE FOREIGN TABLE trktr.pg_log 
                        (
                        log_time timestamp(3) with time zone,
                        user_name text,
                        database_name text,
                        process_id integer,
                        connection_from text,
                        session_id text,
                        session_line_num bigint,
                        command_tag text,
                        session_start_time timestamp with time zone,
                        virtual_transaction_id text,
                        transaction_id bigint,
                        error_severity text,
                        sql_state_code text,
                        message text,
                        detail text,
                        hint text,
                        internal_query text,
                        internal_query_pos integer,
                        context text,
                        query text,
                        query_pos integer,
                        "location" text,
                        application_name text,
                        backend_type text,
                        leader_pid integer,
                        query_id bigint
                        )
                        SERVER trktr_log_server OPTIONS (filename ''' || fn || ''', format ''csv'', encoding ''' || enc || ''')';
                end loop;
        else
                raise exception 'FDW type % is not supported', fdw using hint = 'file_fdw or log_fdw';
        end if;
return query
select
		l.log_time,
        l.sql_state_code,
        l.context,
        l.detail
from
        trktr.pg_log l where l.sql_state_code in ('23505','55000') and backend_type = 'logical replication worker'
order by
        log_time desc;      
drop foreign table if exists trktr.pg_log;
end;$$;
""".format(NODE)


class SubscriptionControl(BaseModel):
    inbound_node: int
    connection_string: Optional[str] = None


class BaseStatus(BaseModel):
    node: int


class TableStatus(BaseModel):
    relation: str
    status: Literal['active', 'pending add', 'pending remove']


class ReplicasetStatus(BaseStatus):
    replicaset: List[TableStatus]


class NodeStatus(BaseStatus):
    replicating: bool
    tainted: bool
    auto_resolved: int
    replication_lag_ms: float
    server_version: float
    pre_16_compatibility: bool


class Resolution(BaseModel):
    occurred: str
    lsn: str
    relation: str
    sql_state_code: str
    resolved: str
    subscription: str
    reason: str


class ResolutionHistory(BaseStatus):
    resolutions: List[Resolution]


app = FastAPI(title="Traktor Arbiter API",
              description="Traktor Arbiter node control API", version=__version__, redoc_url=None)


# def custom_hook(args):
# report the failure
#    print(f'Thread failed: {args.exc_value}')


# set the exception hook
# threading.excepthook = custom_hook


async def api_key_auth(api_key_header: str = Security(api_key_header)):
    """Check the X-API-KEY header."""
    if api_key_header == API_KEY:
        return api_key_header
    else:
        raise HTTPException(
            status_code=403, detail="Could not validate API KEY"
        )


def setup_db_objects():
    """Create all necessary database objects on demand."""
    with closing(psycopg2.connect(CONN_STR)) as conn:
        with conn, conn.cursor() as cur:
            cur.execute(
                SCHEMA)
            logger.debug("Schema")
            cur.execute(
                TABLES)
            logger.debug("Tables")
            cur.execute(
                VIEWS)
            logger.debug("Views")
            cur.execute(
                FUNCTIONS)
            logger.debug("Functions")
            cur.execute(PUBLICATIONS)
            logger.debug("Publications")
            cur.execute("LISTEN trktr_event;")
            logger.debug("trktr_event")
            logger.info("Node created")


def drop_db_objects():
    """Remove all database objects created by setup_db_objects()."""
    try:
        conn = psycopg2.connect(CONN_STR)
        cur = conn.cursor()
        conn.autocommit = True
        cur.execute("""DROP SCHEMA trktr CASCADE;""")
        logger.info("Node dropped")
        cur.execute("""DROP PUBLICATION trktr_pub_multimaster;""")
        cur.execute(
            """SELECT subname FROM pg_subscription WHERE subname like 'trktr_sub_{}_%';""".format(NODE))
        subs = cur.fetchall()
        for sub in subs:
            cur.execute("""DROP SUBSCRIPTION {};""". format(sub[0]))
    finally:
        conn.close()


def get_current_logfile():
    """Get the logfile currently used by the PostgreSQL database server."""
    try:
        with closing(psycopg2.connect(CONN_STR)) as conn:
            with conn, conn.cursor() as cur:
                cur.execute(
                    """SELECT current_logfile_path FROM trktr.v_status;""")
                retval = cur.fetchone()

                return retval[0]
    except Exception as e:
        logger.error(e)

    return None


def check_failed_subscriptions():
    """Find failed SUBSCRIPTIONs used by Traktor, if any."""
    try:
        with closing(psycopg2.connect(CONN_STR)) as conn:
            # Handle the transaction and closing the cursor
            with conn, conn.cursor() as cur:
                cur.execute(
                    """SELECT subname FROM pg_catalog.pg_subscription WHERE not subenabled AND subname like 'trktr_sub_{}_%';""".format(NODE))
                subs = cur.fetchall()
                if not subs:
                    logger.info("No FAILed subscriptions found")
                    return None
                else:
                    logger.info("Subscriptions %s FAILed", subs)
                    return [sub[0] for sub in subs]
    except Exception as e:
        logger.error(e)

    return None


def enable_subscription(sub):
    """ENABLE a given subscription."""
    try:
        with closing(psycopg2.connect(CONN_STR)) as conn:
            # Handle the transaction and closing the cursor
            with conn, conn.cursor() as cur:
                cur.execute(
                    """ALTER SUBSCRIPTION {} ENABLE;""".format(sub))
                logger.info("Subscription %s ENABLED", sub)
    except Exception as e:
        logger.error(e)


def find_new_conflicts_fdw():
    """Find new conflicts and INSERT them into the trktr.history TABLE for deferred resolution."""
    origin_regex = r"pg_\d+"
    relation_regex = r"\w+\.\w+"
    finished_at_LSN_regex = r"([0-9A-Fa-f]+)/([0-9A-Fa-f]+)"

    try:
        with closing(psycopg2.connect(CONN_STR)) as conn:
            # Handle the transaction and closing the cursor
            with conn, conn.cursor() as select_cur, conn.cursor() as insert_cur:
                select_cur.execute(
                    """select log_time, context, detail, sql_state_code from trktr.trktr_find_unresolved_conflicts('{}');""".format(LSN_RESOLVER))
                for line in select_cur:
                    # print(line)
                    timestamp = line[0]
                    context = line[1]
                    detail = line[2]
                    sql_state = line[3]
                    origin = None
                    relation = None
                    lsn = None

                    if context:
                        # print(context)
                        origin_match = search(origin_regex, context)
                        if origin_match:
                            # print("G", origin.group())
                            origin = origin_match.group()
                        relation_match = search(relation_regex, context)
                        if relation_match:
                            # print("R", relation.group())
                            relation = relation_match.group()
                        lsn_match = search(finished_at_LSN_regex, context)
                        if lsn_match:
                            # print("L", lsn.groups())
                            lsn = lsn_match.group()

                        if (origin and timestamp and lsn and relation):
                            insert_cur.execute("""INSERT INTO trktr.history (subscription, occurred, reason, lsn, "relation", sql_state_code) VALUES ((select subname from pg_subscription where ('pg_' || oid) = %s limit 1),%s,%s,%s,%s,%s::int) ON CONFLICT DO NOTHING""", (
                                origin, timestamp, detail, lsn, relation, sql_state))
                            if insert_cur.rowcount == 1:
                                logger.warning(
                                    "Found conflict on Origin: %s, Timestamp: %s, LSN: %s, Relation: %s, Detail: %s, SQLState: %s", origin, timestamp, lsn, relation, detail, sql_state)
                        else:
                            logger.error(
                                "Failed to parse CONTEXT: %s", context)
    except Exception as ex:
        logger.error(ex)


def resolve_conflicts():
    """Resolve new conflicts found in the trktr.history TABLE by advancing the affected SUBSCRIPTION to the next working LSN."""
    try:
        with closing(psycopg2.connect(CONN_STR)) as conn:
            # Handle the transaction and closing the cursor
            with conn, conn.cursor() as cur:
                cur.execute(
                    """SELECT lsn, "subscription", occurred, reason, relation, sql_state_code FROM trktr.history WHERE resolved IS NULL;""")
                unresolved = cur.fetchall()
                # print(unresolved)
                for ur in unresolved:
                    lsn = ur[0]
                    sub = ur[1]
                    timestamp = ur[2]
                    reason = ur[3]
                    relation = ur[4]
                    sql_state = ur[5]

                    if sql_state == '55000':
                        logger.critical(
                            "The cluster may be structurally inconsistent: %s", reason)

                    cur.execute(
                        """ALTER SUBSCRIPTION {} SKIP (lsn = %s);""".format(sub), (lsn,))
                    cur.execute(
                        """UPDATE trktr.history SET resolved = transaction_timestamp() WHERE lsn = %s""", (lsn,))
                    logger.info(
                        "Resolved conflict on Subscription: %s, Timestamp: %s, LSN: %s, Relation: %s, Reason: %s, SQLState: %s", sub, timestamp, lsn, relation, reason, sql_state)
    except Exception as e:
        logger.error(e)


def resolver_thread_function():
    """The actual conflict resolver.
    1.) Find new conflicts IF there are failed SUBSCRIPTIONs
    2.) Resolve recorded conflicts
    3.) Enable failed SUBSCRIPTIONs, if necessary"""
    while threading.main_thread().is_alive():
        subs = check_failed_subscriptions()

        if subs:
            # print(subs)
            find_new_conflicts_fdw()

        resolve_conflicts()

        if subs:
            for sub in subs:
                # print(subs)
                enable_subscription(sub)

        time.sleep(CHECK_INTERVAL)


def sub_watcher_thread_function():
    """Watch local Traktor SUBSCRIPTIONs for changes and start or stop their refresher threads accordingly."""
    evt = None
    current_subs = {}
    try:
        with closing(psycopg2.connect(CONN_STR)) as conn:
            conn.readonly
            with conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT subname, subconninfo FROM pg_catalog.pg_subscription WHERE subname like 'trktr_sub_{}_%';""".format(NODE))
                for sub in cur:
                    evt = threading.Event()
                    cf = threading.Thread(
                        target=refresher_thread_function, args=(evt, sub[0], sub[1]))
                    cf.start()
                    current_subs[sub[0]] = (evt, sub[1], cf)
    except Exception as e:
        logger.error(e)

    while threading.main_thread().is_alive():
        new_subs = {}
        time.sleep(CHECK_INTERVAL)
        try:
            with closing(psycopg2.connect(CONN_STR)) as conn:
                conn.readonly
                with conn, conn.cursor() as cur:
                    cur.execute(
                        "SELECT subname, subconninfo FROM pg_catalog.pg_subscription WHERE subname like 'trktr_sub_{}_%';""".format(NODE))
                    for sub in cur:
                        new_subs[sub[0]] = sub[1]
                    # print(new_subs)

            for subkey in new_subs.keys():
                if subkey not in current_subs:
                    # print("NEW")
                    evt = threading.Event()
                    cf = threading.Thread(
                        target=refresher_thread_function, args=(evt, subkey, new_subs[subkey]))
                    cf.start()
                    current_subs[subkey] = (evt, new_subs[subkey], cf)

            for subkey in current_subs.copy().keys():
                if subkey not in new_subs:
                    # print("REMOVE")
                    current_subs[subkey][0].set()
                    current_subs[subkey][2].join()
                    del current_subs[subkey]

            for subkey in current_subs.keys():
                if not current_subs[subkey][2].is_alive():
                    # print("RESTART failed threads")
                    current_subs[subkey][2].join()
                    cf = threading.Thread(
                        target=refresher_thread_function, args=(current_subs[subkey][0], subkey, current_subs[subkey][1]))
                    cf.start()
                    current_subs[subkey] = (
                        current_subs[subkey][0], current_subs[subkey][1], cf)
        except Exception as e:
            logger.error(e)


def refresher_thread_function(evt, sub, peer_conn_str):
    """LISTEN for changes on a remote PUBLICATION and refresh the affected local SUBSCRIPTION."""
    conn = None
    peer_conn = None
    try:
        peer_conn = psycopg2.connect(peer_conn_str)
        peer_conn.autocommit = True
        conn = psycopg2.connect(CONN_STR)
        conn.autocommit = True
        with peer_conn.cursor() as listen_cur:
            listen_cur.execute("LISTEN trktr_event;")
            while threading.main_thread().is_alive() and (not evt.is_set()):
                logger.info("Refresher %s", sub)
                if select.select([peer_conn], [], [], LISTEN_TIMEOUT) == ([], [], []):
                    logger.info("LISTEN Timeout")
                else:
                    peer_conn.poll()
                    logger.info("Got something")
                    alter_cur = conn.cursor()
                    for notify in peer_conn.notifies:
                        logger.info("Got NOTIFY: %s, %s, %s", notify.pid,
                                    notify.channel, notify.payload)
                        if notify.channel == 'trktr_event':
                            if notify.payload == 'trktr_evt_pubchanged':
                                alter_cur.execute(
                                    'ALTER SUBSCRIPTION {} REFRESH PUBLICATION WITH (copy_data=false)'.format(sub, sub))
                    alter_cur.close()
                    peer_conn.notifies.clear()
    except Exception as e:
        logger.error(e)
    finally:
        if conn:
            conn.close()
        if peer_conn:
            peer_conn.close()


@app.get(COMMON_PATH_V1 + "/resolution/history", response_model=ResolutionHistory, tags=['history'])
async def history(request: Request, api_key: APIKey = Depends(api_key_auth)):
    """API to show the local conflict resolution history."""
    result = result = {'node': NODE, 'resolutions': []}
    with closing(psycopg2.connect(CONN_STR)) as conn:
        # Handle the transaction and closing the cursor
        with conn, conn.cursor() as cur:
            conn.readonly = True
            cur.execute(
                """select json_array(select row_to_json(t) from (select * from trktr.history h order by occurred desc) as t);""")
            r = cur.fetchone()
            if r[0]:
                result['resolutions'] = r[0]

    return result


@app.get(COMMON_PATH_V1 + "/status", response_model=NodeStatus, tags=['status'])
async def status(request: Request, api_key: APIKey = Depends(api_key_auth)):
    """API to show the local arbiter node status."""
    result = {}
    with closing(psycopg2.connect(CONN_STR)) as conn:
        # Handle the transaction and closing the cursor
        with conn, conn.cursor() as cur:
            conn.readonly = True
            cur.execute("""select row_to_json(t) from (select node_id as node, replicating, tainted, auto_resolved, round(avg_replication_lag,3) as replication_lag_ms, server_version, pre_16_compatibility from trktr.v_status) as t;""")
            r = cur.fetchone()
            result = r[0]

    return result


@app.put(COMMON_PATH_V1 + "/control".format(NODE), tags=['init_node'])
@app.delete(COMMON_PATH_V1 + "/control".format(NODE), tags=['drop_node'])
async def node_ctrl(request: Request, api_key: APIKey = Depends(api_key_auth)):
    """API to initialize the local PostgreSQL database server for Traktor, or drop it from the Traktor cluster."""
    try:
        if (request.method == 'PUT'):
            setup_db_objects()
            return Response(status_code=201)
        elif (request.method == 'DELETE'):
            drop_db_objects()
    except Exception as e:
        return Response(status_code=500, content=dumps({'error': str(e)}), media_type="application/json")

    return Response(status_code=200)


@app.get(COMMON_PATH_V1 + "/replicaset/status", response_model=ReplicasetStatus, tags=['replicaset_status'])
async def replicaset_status(request: Request, api_key: APIKey = Depends(api_key_auth)):
    """API to show the local replicaset status."""
    result = []
    with closing(psycopg2.connect(CONN_STR)) as conn:
        # Handle the transaction and closing the cursor
        with conn, conn.cursor() as cur:
            conn.readonly = True
            cur.execute(
                """SELECT schema_name, table_name, table_status FROM trktr.v_replicaset;""")
            for r in cur:
                result.append({'relation': '{}.{}'.format(r[0], r[1]),
                               'status': r[2]})

    return {'node': NODE, 'replicaset': result}


@app.put(COMMON_PATH_V1 + "/subscription/control", tags=['add_subscription'])
@app.delete(COMMON_PATH_V1 + "/subscription/control", tags=['remove_subscription'])
async def sub_ctrl(request: Request, control: SubscriptionControl, api_key: APIKey = Depends(api_key_auth)):
    """API to add or remove a Traktor SUBSCRIPTION to/from the local PostgreSQL database server."""
    sql = None
    try:
        conn = psycopg2.connect(CONN_STR)
        conn.autocommit = True
        cur = conn.cursor()

        if (request.method == 'PUT'):
            # print("PUT")
            cur.execute("""SELECT current_setting('server_version')::float;""")
            server_version = cur.fetchone()[0]
            # print(server_version)
            sub_name = "trktr_sub_{}_{}".format(NODE, control.inbound_node)
            if server_version < 16.0:
                sql = """CREATE SUBSCRIPTION {} CONNECTION '{}' PUBLICATION trktr_pub_multimaster WITH (copy_data = false, enabled = true, disable_on_error = true);"""
            elif PRE_16_COMPATIBILITY:
                sql = """CREATE SUBSCRIPTION {} CONNECTION '{}' PUBLICATION trktr_pub_multimaster WITH (copy_data = false, enabled = true, origin = any, disable_on_error = true);"""
            else:
                sql = """CREATE SUBSCRIPTION {} CONNECTION '{}' PUBLICATION trktr_pub_multimaster WITH (copy_data = false, enabled = true, origin = none, disable_on_error = true);"""

            sql = sql.format(sub_name,
                             control.connection_string)
            # print(sql)
            cur.execute(sql)
            return Response(status_code=201)
        elif (request.method == 'DELETE'):
            sql = """DROP SUBSCRIPTION trktr_sub_{}_{};""".format(
                NODE, control.source_node)
            cur.execute(sql)
    except Exception as e:
        return Response(status_code=500, content=dumps({'error': str(e)}), media_type="application/json")
    finally:
        conn.close()

    return Response(status_code=201)


@app.patch(COMMON_PATH_V1 + "/replicaset".format(NODE), tags=['replicaset_commit'])
async def repset(request: Request, api_key: APIKey = Depends(api_key_auth)):
    """COMMIT a local replicaset."""
    with closing(psycopg2.connect(CONN_STR)) as conn:
        with conn, conn.cursor() as cur:
            try:
                cur.execute(
                    'CALL trktr.trktr_commit_replicaset()')
            except Exception as e:
                return Response(status_code=500, content=dumps({'error': str(e)}), media_type="application/json")

    return Response(status_code=200)


@app.put(COMMON_PATH_V1 + "/replicaset/{table}", tags=['replicaset_add_table'])
@app.delete(COMMON_PATH_V1 + "/replicaset/{table}", tags=['replicaset_remove_table'])
async def repset_table(table: str, request: Request, api_key: APIKey = Depends(api_key_auth)):
    """Add or remove a TABLE to/from the local replicaset."""
    with closing(psycopg2.connect(CONN_STR)) as conn:
        with conn, conn.cursor() as cur:
            parts = table.split('.')
            if len(parts) != 2:
                raise HTTPException(
                    status_code=400,
                    detail="Malformed table expression",
                )
            if (request.method == 'PUT'):
                try:
                    cur.execute(
                        'CALL trktr.trktr_add_table_to_replicaset(%s, %s)', (parts[0], parts[1]))
                except Exception as e:
                    return Response(status_code=409, content=dumps({'error': str(e)}), media_type="application/json")
                return Response(status_code=201)
            elif (request.method == 'DELETE'):
                try:
                    cur.execute(
                        'CALL trktr.trktr_remove_table_from_replicaset(%s, %s)', (parts[0], parts[1]))
                except Exception as e:
                    return Response(status_code=409, content=dumps({'error': str(e)}), media_type="application/json")

    return Response(status_code=200)

if __name__ == "__main__":
    """Start all houskeeping threads and serve the API."""
    if LSN_RESOLVER in ('file_fdw', 'log_fdw'):
        ct = threading.Thread(target=resolver_thread_function, args=())
        ct.start()

    wt = threading.Thread(target=sub_watcher_thread_function, args=())
    wt.start()

    if SSL_CERTFILE and SSL_KEYFILE:
        uvicorn.run("arbiter:app", host=API_HOST, port=int(API_PORT), reload=False,
                    ssl_keyfile=SSL_KEYFILE, ssl_certfile=SSL_CERTFILE)
    else:
        uvicorn.run("arbiter:app", host=API_HOST,
                    port=int(API_PORT), reload=False)
