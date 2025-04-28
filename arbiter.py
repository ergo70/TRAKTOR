#!/usr/bin/env python
# -*- coding: utf-8 -*-

# MIT License
#
# Copyright (c) 2023, 2025 Ernst-Georg Schmid
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

import pg8000.native
import time
import uvicorn
import configparser
import threading
import logging
import ssl
from sys import exit
from pg8000.core import DatabaseError
from fastapi import FastAPI, Request, Response, HTTPException, Depends, Security
from pydantic import BaseModel
from typing import Literal, List, Optional
from json import dumps
from re import search
from fastapi.security.api_key import APIKey, APIKeyHeader

__author__ = """Ernst-Georg Schmid"""
__copyright__ = """Copyright 2023, 2025 Ernst-Georg Schmid"""
__credits__ = ["""Ernst-Georg Schmid"""]
__license__ = """MIT"""
__version__ = """2.0.0"""
__maintainer__ = """Ernst-Georg Schmid"""
__email__ = """pgchem@tuschehund.de"""
__status__ = """EXPERIMENTAL"""


class SubscriptionControl(BaseModel):
    inbound_node: int
    connection_string: Optional[str] = None


class BaseStatus(BaseModel):
    node: int


class TableStatus(BaseModel):
    relation: str
    status: Literal["""committed""", """pending-add""", """pending-remove"""]


class ReplicasetStatus(BaseStatus):
    replicaset: List[TableStatus]


class NodeStatus(BaseStatus):
    is_replicating: bool
    is_tainted: bool
    conflicts_pending: int
    conflicts_resolved: int
    replication_lag_ms: float
    server_version: float


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


class DatabaseConnInfo(BaseModel):
    dbname: str = """postgres"""
    host: str = """localhost"""
    user: Optional[str] = """trktr_arbiter"""
    port: Optional[int] = 5432
    password: Optional[str] = None
    sslcert: Optional[str] = None
    sslkey: Optional[str] = None
    sslrootcert: Optional[str] = None


tags_metadata = [
    {
        """name""": """status""",
        """description""": """Node status.""",
    },
    {
        """name""": """init_node""",
        """description""": """Init Node.""",
    },
    {
        """name""": """history""",
        """description""": """Auto resolution history.""",
    },
    {
        """name""": """drop_node""",
        """description""": """Drop Node.""",
    },
    {
        """name""": """replicaset_status""",
        """description""": """Replicaset status.""",
    },
    {
        """name""": """replication_control""",
        """description""": """Replication control.""",
    },
    {
        """name""": """add_subscription""",
        """description""": """Add Subscription.""",
    },
    {
        """name""": """remove_subscription""",
        """description""": """Remove Subscription.""",
    },
    {
        """name""": """replicaset_commit""",
        """description""": """COMMIT the replicaset.""",
    },
    {
        """name""": """replicaset_add_table""",
        """description""": """Add table to replicaset.""",
    },
    {
        """name""": """replicaset_remove_table""",
        """description""": """Remove table from replicaset.""",
    },
]

config = configparser.ConfigParser()
config.read("""arbiter.conf""")


def parse_connection_string(connection_string: str) -> DatabaseConnInfo:
    """Parse the connection string into a dictionary."""
    return DatabaseConnInfo.model_validate({k.lower(): v.lower() for k, v in (
        c.split('=') for c in connection_string.split())})


def get_pg_connection(database: str, host: str, port: int, user: str = None, password: str = None, sslcert: str = None, sslkey: str = None, sslrootcert: str = None, read_only: bool = False) -> pg8000.native.Connection:
    """Get a secured connection to the database."""
    conn = None

    if (user and password):
        # Simple connection with user and password and relaxed SSL settings
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        conn = pg8000.native.Connection(
            database=database, host=host, port=port, user=user, password=password, ssl_context=ssl_context)
    elif (sslcert and sslkey and sslrootcert):
        # Secure connection with mutual SSL certificates (also for identifying the USER) and strict SSL settings
        ssl_context = ssl.create_default_context(
            cafile=sslrootcert, keyfile=sslkey, certfile=sslcert)
        ssl_context.verify_mode = ssl.CERT_REQUIRED
        ssl_context.check_hostname = True
        conn = pg8000.native.Connection(
            database=database, host=host, port=port, ssl_context=ssl_context)

    if (conn and read_only):
        conn.run("""SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;""")

    return conn


PG_MIN_VERSION = 160000
CHECK_INTERVAL = config["""DEFAULT"""].getint("""CheckInterval""", 10)
NODE = config["""DEFAULT"""].getint("""NodeID""")
DB_CONN_INFO = parse_connection_string(
    config["""DEFAULT"""].get("""ConnectionString"""))
API_ADDRESS = config["""DEFAULT"""]["""APIAddress"""]
API_HOST, API_PORT = API_ADDRESS.split(':')
API_KEY = config["""DEFAULT"""]["""APIKey"""]
LSN_RESOLVER = config["""DEFAULT"""].get("""LSNResolver""", """""").lower()
SSL_KEYFILE = config["""DEFAULT"""].get("""SSLKeyfile""")
SSL_CERTFILE = config["""DEFAULT"""].get("""SSLCertfile""")

COMMON_PATH_V1 = """/v1/arbiter"""

# setup loggers
logging.config.fileConfig("""logging.conf""", disable_existing_loggers=False)
logger = logging.getLogger(__name__)

api_key_header = APIKeyHeader(name="""X-API-KEY""", auto_error=False)

PERMISSIONS = f"""
GRANT CREATE ON DATABASE {DB_CONN_INFO.dbname} TO {DB_CONN_INFO.user};
GRANT pg_create_subscription TO {DB_CONN_INFO.user};
GRANT pg_read_all_settings TO {DB_CONN_INFO.user};
GRANT pg_read_server_files TO {DB_CONN_INFO.user};
GRANT SELECT ON pg_subscription TO {DB_CONN_INFO.user};
"""

if LSN_RESOLVER in ["""file_fdw""", """log_fdw"""]:
    PERMISSIONS += f"""CREATE EXTENSION IF NOT EXISTS {LSN_RESOLVER};GRANT USAGE ON FOREIGN DATA WRAPPER {LSN_RESOLVER} TO {DB_CONN_INFO.user};"""
    if LSN_RESOLVER == """file_fdw""":
        PERMISSIONS += f"""GRANT EXECUTE ON FUNCTION pg_ls_logdir() TO {DB_CONN_INFO.user};"""
    elif LSN_RESOLVER == """log_fdw""":
        PERMISSIONS += f"""GRANT EXECUTE ON FUNCTION list_postgres_log_files() TO {DB_CONN_INFO.user};"""

SCHEMA = f"""CREATE SCHEMA IF NOT EXISTS trktr AUTHORIZATION {DB_CONN_INFO.user};"""

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

PUBLICATIONS = """DO $$ BEGIN IF NOT EXISTS (SELECT true FROM pg_publication WHERE pubname = 'trktr_pub_66fbce89_1855_4593_91f0_f9047edd1306') THEN CREATE PUBLICATION trktr_pub_66fbce89_1855_4593_91f0_f9047edd1306; END IF; END $$;"""

VIEWS = f"""
CREATE OR REPLACE VIEW trktr.v_peer_node_replication_distance AS
SELECT
slot_name,
split_part(slot_name, '_', 3) AS peer_node,
(pg_current_wal_lsn() - confirmed_flush_lsn) AS lsn_distance_bytes
FROM pg_replication_slots;
CREATE OR REPLACE VIEW trktr.v_status
 AS SELECT {NODE} as node_id,
 not exists ((SELECT true FROM pg_subscription WHERE not subenabled AND subname like 'trktr_sub_{NODE}_%' limit 1)) as is_replicating,
 exists ((SELECT true FROM trktr.history limit 1)) as is_tainted,
 (select count(*) from trktr.history where resolved is not null) as conflicts_resolved,
( SELECT count(*) AS count
           FROM trktr.history
          WHERE history.resolved IS NULL) AS conflicts_pending,
COALESCE((select avg(1000 * extract(epoch from (last_msg_receipt_time - last_msg_send_time))) from pg_stat_subscription where subname like 'trktr_sub_{NODE}_%'), 0.0) as avg_replication_lag,
(select current_setting('server_version')::float) as server_version;
create or replace view trktr.v_replicaset as
select schemaname as schema_name, tablename as table_name, 'committed' as table_status from pg_catalog.pg_publication_tables t
union
select table_schema, table_name, 'pending-add' from
(SELECT table_schema, table_name FROM trktr.replicaset r except
select schemaname, tablename from pg_catalog.pg_publication_tables) as t
union
select schemaname, tablename, 'pending-remove' from
(select schemaname, tablename from pg_catalog.pg_publication_tables except
SELECT table_schema, table_name FROM trktr.replicaset r) as t;"""

FUNCTIONS = f"""
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
        execute 'alter publication trktr_pub_66fbce89_1855_4593_91f0_f9047edd1306 add table ' || fqt;
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
		execute 'alter publication trktr_pub_66fbce89_1855_4593_91f0_f9047edd1306 drop table ' || fqt;
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
	if not exists((select true FROM pg_tables t where t.tablename = tbl and t.schemaname = sh)) then
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
if (select count(*) < 1 from trktr.v_replicaset) then
	raise exception 'The replicaset is empty';
end if;
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
 RETURNS TABLE(log_time timestamp with time zone, sql_state_code text, context text, detail text, message text)
 LANGUAGE plpgsql
 STRICT
AS $$
declare
r record;
fn text;
enc text := 'utf8';
begin
drop foreign table if exists trktr.pg_log;
	if version() ilike any (array['%Windows%', '%mingw%', '%Visual%']) then
		enc := 'latin1';
	end if;
        if fdw = 'log_fdw' then
                CREATE SERVER if not exists trktr_log_server FOREIGN DATA WRAPPER log_fdw;
                for r in SELECT file_name FROM list_postgres_log_files() where file_name LIKE 'postgresql%csv' ORDER BY 1 desc limit 1 loop
                        SELECT create_foreign_table_for_log_file('trktr.pg_log', 'trktr_log_server', r.file_name);
                end loop;
        elseif fdw = 'file_fdw' then
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
        l.detail,
        l.message
from
        trktr.pg_log l where l.sql_state_code in ('23505','55000') and backend_type = 'logical replication worker'
order by
        log_time desc;
drop foreign table if exists trktr.pg_log;
end;$$;
"""

EVENT_TRIGGERS = """
create or replace
function trktr.trktr_tf_protect_drop_replicaset()
  returns event_trigger
 language plpgsql
  as $$
  declare
  	r record;

begin
  		for r in
select
	schema_name,
	object_name,
	object_identity
from
	pg_event_trigger_dropped_objects() loop
	  		if exists(
	select
		true
	from
		trktr.replicaset
	where
		table_schema = r.schema_name
		and table_name = r.object_name) then
	   			RAISE WARNING E'Cannot DROP a replicated table %', r.object_identity using HINT = 'Table is immutable part of a TRAKTOR replicaset';
raise exception sqlstate '55006';
end if;
end loop;
end;
$$;
CREATE EVENT TRIGGER pgc_evtrg_trktr_protect_drop_replicaset ON sql_drop WHEN TAG IN ('DROP TABLE')
   EXECUTE FUNCTION trktr.trktr_tf_protect_drop_replicaset();
  CREATE OR REPLACE FUNCTION trktr.trktr_tf_protect_alter_replicaset()
 RETURNS event_trigger
 LANGUAGE plpgsql
AS $function$
  declare
  	r record;
begin
  		for r in select object_identity from pg_event_trigger_ddl_commands() loop
	  		if exists(select true from trktr.replicaset where table_schema || '.' || table_name = r.object_identity) then
	   			RAISE WARNING E'Cannot ALTER a replicated table %', r.object_identity using HINT = 'Table is immutable part of a TRAKTOR replicaset';
				RAISE exception SQLSTATE '55006';
			end if;
  		end loop;
END;
$function$;
CREATE EVENT TRIGGER pgc_evtrg_trktr_protect_alter_replicaset ON ddl_command_end WHEN TAG IN ('ALTER TABLE')
	EXECUTE FUNCTION trktr.trktr_tf_protect_alter_replicaset();
"""

DROP_SUPERUSER_RIGHTS = f"""ALTER ROLE {DB_CONN_INFO.user} NOSUPERUSER LOGIN REPLICATION;"""


app = FastAPI(title="""Traktor Arbiter API""",
              description="""Traktor Arbiter node control API""", version=__version__, redoc_url=None)


async def api_key_auth(api_key_header: str = Security(api_key_header)):
    """Check the X-API-KEY header."""
    if api_key_header == API_KEY:
        return api_key_header
    else:
        raise HTTPException(
            status_code=403, detail="""Could not validate API key."""
        )


def setup_db_objects():
    """Create all necessary database objects on demand."""
    conn = None
    try:
        conn = get_pg_connection(database=DB_CONN_INFO.dbname, host=DB_CONN_INFO.host, port=DB_CONN_INFO.port, user=DB_CONN_INFO.user,
                                 password=DB_CONN_INFO.password, sslcert=DB_CONN_INFO.sslcert, sslkey=DB_CONN_INFO.sslkey, sslrootcert=DB_CONN_INFO.sslrootcert)
        conn.run(PERMISSIONS)
        logger.debug("""Permissions""")
        conn.run(SCHEMA)
        logger.debug("""Schema""")
        conn.run(TABLES)
        logger.debug("""Tables""")
        conn.run(
            VIEWS)
        logger.debug("""Views""")
        conn.run(
            FUNCTIONS)
        logger.debug("""Functions""")
        conn.run(PUBLICATIONS)
        logger.debug("""Publications""")
        conn.run(EVENT_TRIGGERS)
        logger.debug("""Event triggers""")
        conn.run(DROP_SUPERUSER_RIGHTS)
        logger.debug("""Drop superuser rights""")
        logger.info("""Node created""")
    finally:
        if conn:
            conn.close()


def drop_db_objects():
    """Remove all database objects created by setup_db_objects()."""
    conn = None
    try:
        conn = get_pg_connection(
            database=DB_CONN_INFO.dbname, host=DB_CONN_INFO.host, port=DB_CONN_INFO.port, user=DB_CONN_INFO.user, password=DB_CONN_INFO.password, sslcert=DB_CONN_INFO.sslcert, sslkey=DB_CONN_INFO.sslkey, sslrootcert=DB_CONN_INFO.sslrootcert)
        conn.run("""DROP SCHEMA trktr CASCADE;""")
        logger.info("""Node: %s dropped""", NODE)
        conn.run(
            """DROP PUBLICATION trktr_pub_66fbce89_1855_4593_91f0_f9047edd1306;""")

        for subscription in conn.run(
                f"""SELECT subname FROM pg_subscription WHERE subname like 'trktr_sub_{NODE}_%';"""):
            conn.run(f"""DROP SUBSCRIPTION {subscription[0]};""")
    finally:
        if conn:
            conn.close()


def check_failed_subscriptions():
    """Find failed SUBSCRIPTIONs used by Traktor, if any."""
    conn = None
    try:
        conn = conn = get_pg_connection(
            database=DB_CONN_INFO.dbname, host=DB_CONN_INFO.host, port=DB_CONN_INFO.port, user=DB_CONN_INFO.user, password=DB_CONN_INFO.password, sslcert=DB_CONN_INFO.sslcert, sslkey=DB_CONN_INFO.sslkey, sslrootcert=DB_CONN_INFO.sslrootcert, read_only=True)
        subscriptions = conn.run(
            f"""SELECT subname FROM pg_catalog.pg_subscription WHERE not subenabled AND subname like 'trktr_sub_{NODE}_%';""")
        if not subscriptions:
            logger.info("""No FAILed subscriptions found""")
            return None
        else:
            logger.info("""Subscriptions %s FAILed""", subscriptions)
            return [subscription[0] for subscription in subscriptions]
    except Exception as e:
        logger.error(e)
    finally:
        if conn:
            conn.close()

    return None


def enable_subscription(subscription):
    """ENABLE a given subscription."""
    conn = None
    try:
        conn = conn = get_pg_connection(
            database=DB_CONN_INFO.dbname, host=DB_CONN_INFO.host, port=DB_CONN_INFO.port, user=DB_CONN_INFO.user, password=DB_CONN_INFO.password, sslcert=DB_CONN_INFO.sslcert, sslkey=DB_CONN_INFO.sslkey, sslrootcert=DB_CONN_INFO.sslrootcert)
        conn.run(f"""ALTER SUBSCRIPTION {subscription} ENABLE;""")
        logger.info("""Subscription %s ENABLED""", subscription)
    except Exception as e:
        logger.error(e)
    finally:
        if conn:
            conn.close()


def find_new_conflicts_fdw():
    """Find new conflicts and INSERT them into the trktr.history TABLE for deferred resolution."""
    conn = None
    origin_regex = r"""pg_\d+"""
    relation_regex = r"""\w+\.\w+"""
    finished_at_LSN_regex = r"""([0-9A-Fa-f]+)/([0-9A-Fa-f]+)"""

    try:
        conn = conn = get_pg_connection(
            database=DB_CONN_INFO.dbname, host=DB_CONN_INFO.host, port=DB_CONN_INFO.port, user=DB_CONN_INFO.user, password=DB_CONN_INFO.password, sslcert=DB_CONN_INFO.sslcert, sslkey=DB_CONN_INFO.sslkey, sslrootcert=DB_CONN_INFO.sslrootcert)
        select_cur = conn.run(
            f"""select log_time, context, detail, sql_state_code, message from trktr.trktr_find_unresolved_conflicts('{LSN_RESOLVER}');""")
        for line in select_cur:
            # print(line)
            timestamp = line[0]
            context = line[1]
            detail = line[2]
            sql_state = line[3]
            message = line[4]
            origin = None
            relation = None
            lsn = None
            reason = None

            if context:
                # print(context, detail)
                origin_match = search(origin_regex, context)
                if origin_match:
                    # print("G", origin_match.group())
                    origin = origin_match.group()
                relation_match = search(relation_regex, context)
                if relation_match:
                    reason = detail
                    # print("R", relation_match.group())
                    relation = relation_match.group()
                else:
                    relation_match = search(relation_regex, message)
                if relation_match:
                    reason = message
                    # print("R2", relation_match.group())
                    relation = relation_match.group()
                lsn_match = search(finished_at_LSN_regex, context)
                if lsn_match:
                    # print("L", lsn_match.group())
                    lsn = lsn_match.group()

                if (origin and timestamp and lsn and relation and sql_state and reason):
                    conn.run("""INSERT INTO trktr.history (subscription, occurred, reason, lsn, "relation", sql_state_code) VALUES ((select subname from pg_subscription where ('pg_' || oid) = :origin limit 1),:timestamp,:reason,:lsn,:relation,:sql_state) ON CONFLICT DO NOTHING""",
                             origin=origin, timestamp=timestamp, reason=reason, lsn=lsn, relation=relation, sql_state=sql_state)
                    if conn.row_count == 1:
                        logger.warning(
                            """Found conflict on Origin: %s, Timestamp: %s, LSN: %s, Relation: %s, Reason: %s, SQLState: %s""", origin, timestamp, lsn, relation, reason, sql_state)
            else:
                logger.error(
                    """Failed to parse CONTEXT: %s""", context)
    except Exception as e:
        logger.error(e)
    finally:
        if conn:
            conn.close()


def resolve_conflicts():
    """Resolve new conflicts found in the trktr.history TABLE by advancing the affected SUBSCRIPTION to the next working LSN."""
    conn = None
    try:
        conn = conn = get_pg_connection(
            database=DB_CONN_INFO.dbname, host=DB_CONN_INFO.host, port=DB_CONN_INFO.port, user=DB_CONN_INFO.user, password=DB_CONN_INFO.password, sslcert=DB_CONN_INFO.sslcert, sslkey=DB_CONN_INFO.sslkey, sslrootcert=DB_CONN_INFO.sslrootcert)
        unresolved = conn.run(
            """SELECT lsn, "subscription", occurred, reason, relation, sql_state_code FROM trktr.history WHERE resolved IS NULL;""")

        # print(unresolved)
        for ur in unresolved:
            lsn = ur[0]
            # print(lsn)
            subscription = ur[1]
            # print(subscription)
            timestamp = ur[2]
            # print(timestamp)
            reason = ur[3]
            # print(reason)
            relation = ur[4]
            # print(relation)
            sql_state = ur[5]
            # print(sql_state)
            if sql_state == """55000""":
                logger.critical(
                    """The cluster may be structurally inconsistent: %s""", reason)
            try:
                conn.run(
                    f"""ALTER SUBSCRIPTION {subscription} SKIP (lsn = '{lsn}');""")
            except DatabaseError as dbe:
                if str(dbe).find('must be greater than origin LSN') != -1:
                    pass
                else:
                    logger.error(dbe)
            conn.run(
                """UPDATE trktr.history SET resolved = transaction_timestamp() WHERE lsn = :lsn;""", lsn=lsn)
            logger.info(
                """Resolved conflict on Subscription: %s, Timestamp: %s, LSN: %s, Relation: %s, Reason: %s, SQLState: %s""", subscription, timestamp, lsn, relation, reason, sql_state)
    except Exception as e:
        logger.error(e)
    finally:
        if conn:
            conn.close()


def resolver_thread_function():
    """The actual conflict resolver.
    1.) Find new conflicts IF there are failed SUBSCRIPTIONs
    2.) Resolve recorded conflicts
    3.) Enable failed SUBSCRIPTIONs, if necessary"""
    while threading.main_thread().is_alive():
        subscriptions = check_failed_subscriptions()

        if subscriptions:
            # print(subscriptions)
            find_new_conflicts_fdw()

        resolve_conflicts()

        if subscriptions:
            for subscription in subscriptions:
                # print(subscription)
                enable_subscription(subscription)

        time.sleep(CHECK_INTERVAL)


def sub_watcher_thread_function():
    """Watch local Traktor SUBSCRIPTIONs for changes and start or stop their refresher threads accordingly."""
    conn = None
    event = None
    current_subscriptions = {}
    try:
        conn = conn = get_pg_connection(
            database=DB_CONN_INFO.dbname, host=DB_CONN_INFO.host, port=DB_CONN_INFO.port, user=DB_CONN_INFO.user, password=DB_CONN_INFO.password, sslcert=DB_CONN_INFO.sslcert, sslkey=DB_CONN_INFO.sslkey, sslrootcert=DB_CONN_INFO.sslrootcert, read_only=True)

        subscriptions = conn.run(
            f"""SELECT subname, subconninfo FROM pg_catalog.pg_subscription WHERE subname like 'trktr_sub_{NODE}_%';""")
        for subscription in subscriptions:
            event = threading.Event()
            cf = threading.Thread(target=refresher_thread_function, args=(
                event, subscription[0], subscription[1]))
            cf.start()
            current_subscriptions[subscription[0]] = (
                event, subscription[1], cf)
    except Exception as e:
        logger.error(e)
    finally:
        if conn:
            conn.close()

    while threading.main_thread().is_alive():
        new_subscriptions = {}
        time.sleep(CHECK_INTERVAL)
        try:
            conn = get_pg_connection(database=DB_CONN_INFO.dbname, host=DB_CONN_INFO.host,
                                     port=DB_CONN_INFO.port, user=DB_CONN_INFO.user, password=DB_CONN_INFO.password, sslcert=DB_CONN_INFO.sslcert, sslkey=DB_CONN_INFO.sslkey, sslrootcert=DB_CONN_INFO.sslrootcert, read_only=True)
            for subscription in conn.run(
                    f"""SELECT subname, subconninfo FROM pg_catalog.pg_subscription WHERE subname like 'trktr_sub_{NODE}_%';"""):
                new_subscriptions[subscription[0]] = subscription[1]
                # print(new_subs)

            for sub_key in new_subscriptions.keys():
                if sub_key not in current_subscriptions:
                    # print("NEW")
                    event = threading.Event()
                    cf = threading.Thread(
                        target=refresher_thread_function, args=(event, sub_key, new_subscriptions[sub_key]))
                    cf.start()
                    current_subscriptions[sub_key] = (
                        event, new_subscriptions[sub_key], cf)

            for sub_key in current_subscriptions.copy().keys():
                if sub_key not in new_subscriptions:
                    # print("REMOVE")
                    current_subscriptions[sub_key][0].set()
                    current_subscriptions[sub_key][2].join()
                    del current_subscriptions[sub_key]

            for sub_key in current_subscriptions.keys():
                if not current_subscriptions[sub_key][2].is_alive():
                    # print("RESTART failed threads")
                    current_subscriptions[sub_key][2].join()
                    cf = threading.Thread(
                        target=refresher_thread_function, args=(current_subscriptions[sub_key][0], sub_key, current_subscriptions[sub_key][1]))
                    cf.start()
                    current_subscriptions[sub_key] = (
                        current_subscriptions[sub_key][0], current_subscriptions[sub_key][1], cf)
        except Exception as e:
            logger.error(e)


def refresher_thread_function(event, subscription, peer_conn_string):
    """LISTEN for changes on a remote PUBLICATION and refresh the affected local SUBSCRIPTION."""
    local_conn = None
    peer_conn = None
    peer_conn_info = parse_connection_string(peer_conn_string)
    try:
        peer_conn = get_pg_connection(database=peer_conn_info.dbname, host=peer_conn_info.host,
                                      port=peer_conn_info.port, user=peer_conn_info.user, password=peer_conn_info.password, sslcert=peer_conn_info.sslcert, sslkey=peer_conn_info.sslkey, sslrootcert=peer_conn_info.sslrootcert, read_only=True)
        # peer_conn.autocommit = True
        peer_conn.run("""LISTEN trktr_event;""")
        while threading.main_thread().is_alive() and (not event.is_set()):
            time.sleep(CHECK_INTERVAL)
            logger.info("""Refresher %s""", subscription)
            peer_conn.run(
                """SELECT;""")  # Poll the server to get notifications, if any
            while len(peer_conn.notifications) > 0:
                notification = peer_conn.notifications.pop()
                logger.info("""Got NOTIFY: %s, %s, %s""", notification[0],
                            notification[1], notification[2])
                if notification[1] == """trktr_event""":
                    if notification[2] == """trktr_evt_pubchanged""":
                        local_conn = get_pg_connection(
                            database=DB_CONN_INFO.dbname, host=DB_CONN_INFO.host, port=DB_CONN_INFO.port, user=DB_CONN_INFO.user, password=DB_CONN_INFO.password, sslcert=DB_CONN_INFO.sslcert, sslkey=DB_CONN_INFO.sslkey, sslrootcert=DB_CONN_INFO.sslrootcert)
                        # local_conn.autocommit = True
                        local_conn.run(
                            f"""ALTER SUBSCRIPTION {subscription} REFRESH PUBLICATION WITH (copy_data=false);""")
    except Exception as e:
        logger.error(e)
    finally:
        if local_conn:
            local_conn.close()
        if peer_conn:
            peer_conn.close()


def is_server_compatible():
    """Check server compatibility."""
    conn = None
    try:
        conn = conn = get_pg_connection(
            database=DB_CONN_INFO.dbname, host=DB_CONN_INFO.host, port=DB_CONN_INFO.port, user=DB_CONN_INFO.user, password=DB_CONN_INFO.password, sslcert=DB_CONN_INFO.sslcert, sslkey=DB_CONN_INFO.sslkey, sslrootcert=DB_CONN_INFO.sslrootcert, read_only=True)
        pg_version = conn.run(
            """SHOW server_version_num;""")  # Get comparable server version integer
        if int(pg_version[0][0]) < PG_MIN_VERSION:
            logger.fatal(
                """PostgreSQL versions < %s are not compatible with Traktor.""", PG_MIN_VERSION)
            return False
    except Exception as e:
        logger.error(e)
    finally:
        if conn:
            conn.close()

    return True


@app.get(COMMON_PATH_V1 + "/resolution/history", response_model=ResolutionHistory, tags=["""history"""])
async def history(request: Request, api_key: APIKey = Depends(api_key_auth)):
    """API to show the local conflict resolution history."""
    result = {"""node""": NODE, """resolutions""": []}
    try:
        conn = conn = get_pg_connection(
            database=DB_CONN_INFO.dbname, host=DB_CONN_INFO.host, port=DB_CONN_INFO.port, user=DB_CONN_INFO.user, password=DB_CONN_INFO.password, sslcert=DB_CONN_INFO.sslcert, sslkey=DB_CONN_INFO.sslkey, sslrootcert=DB_CONN_INFO.sslrootcert, read_only=True)
        r = conn.run(
            """select json_array(select row_to_json(t) from (select * from trktr.history h order by occurred desc) as t);""")
        if r[0]:
            result["""resolutions"""] = r[0]
    except Exception as e:
        logger.error(e)
    finally:
        if conn:
            conn.close()

    return result


@app.get(COMMON_PATH_V1 + """/status""", response_model=NodeStatus, tags=["""status"""])
async def status(request: Request, api_key: APIKey = Depends(api_key_auth)):
    """API to show the local arbiter node status."""
    result = {}
    try:
        conn = conn = get_pg_connection(
            database=DB_CONN_INFO.dbname, host=DB_CONN_INFO.host, port=DB_CONN_INFO.port, user=DB_CONN_INFO.user, password=DB_CONN_INFO.password, sslcert=DB_CONN_INFO.sslcert, sslkey=DB_CONN_INFO.sslkey, sslrootcert=DB_CONN_INFO.sslrootcert, read_only=True)
        r = conn.run("""select row_to_json(t) from (select node_id as node, is_replicating, is_tainted, conflicts_pending, conflicts_resolved, round(avg_replication_lag,3) as replication_lag_ms, server_version from trktr.v_status) as t LIMIT 1;""")
        result = r[0][0]
    except Exception as e:
        logger.error(e)
    finally:
        if conn:
            conn.close()

    return result


@app.put(COMMON_PATH_V1 + """/control""", tags=["""init_node"""])
@app.delete(COMMON_PATH_V1 + """/control""", tags=["""drop_node"""])
async def node_ctrl(request: Request, api_key: APIKey = Depends(api_key_auth)):
    """API to initialize the local PostgreSQL database server for Traktor, or drop it from the Traktor cluster."""
    try:
        if (request.method == """PUT"""):
            setup_db_objects()
            return Response(status_code=201)
        elif (request.method == """DELETE"""):
            drop_db_objects()
    except Exception as e:
        return Response(status_code=500, content=dumps({"""error""": str(e)}), media_type="""application/json""")

    return Response(status_code=200)


@app.get(COMMON_PATH_V1 + """/replicaset/status""", response_model=ReplicasetStatus, tags=["""replicaset_status"""])
async def replicaset_status(request: Request, api_key: APIKey = Depends(api_key_auth)):
    """API to show the local replicaset status."""
    result = []
    try:
        # Handle the transaction and closing the cursor
        conn = conn = get_pg_connection(
            database=DB_CONN_INFO.dbname, host=DB_CONN_INFO.host, port=DB_CONN_INFO.port, user=DB_CONN_INFO.user, password=DB_CONN_INFO.password, sslcert=DB_CONN_INFO.sslcert, sslkey=DB_CONN_INFO.sslkey, sslrootcert=DB_CONN_INFO.sslrootcert, read_only=True)
        for r in conn.run("""SELECT schema_name, table_name, table_status FROM trktr.v_replicaset;"""):
            result.append({"""relation""": f"""{r[0]}.{r[1]}""",
                           """status""": r[2]})
    except Exception as e:
        logger.error(e)
    finally:
        if conn:
            conn.close()

    return {"""node""": NODE, """replicaset""": result}


@app.put(COMMON_PATH_V1 + """/subscription/control""", tags=["""add_subscription"""])
@app.delete(COMMON_PATH_V1 + """/subscription/control""", tags=["""remove_subscription"""])
async def sub_ctrl(request: Request, control: SubscriptionControl, api_key: APIKey = Depends(api_key_auth)):
    """API to add or remove a Traktor SUBSCRIPTION to/from the local PostgreSQL database server."""
    sql = None
    try:
        conn = conn = get_pg_connection(
            database=DB_CONN_INFO.dbname, host=DB_CONN_INFO.host, port=DB_CONN_INFO.port, user=DB_CONN_INFO.user, password=DB_CONN_INFO.password, sslcert=DB_CONN_INFO.sslcert, sslkey=DB_CONN_INFO.sslkey, sslrootcert=DB_CONN_INFO.sslrootcert)
        # conn.autocommit = True

        if (request.method == """PUT"""):
            # print("PUT")
            server_version = conn.run(
                """SELECT current_setting('server_version')::float;""")
            server_version = server_version[0][0]
            # print(server_version)
            sub_name = f"""trktr_sub_{NODE}_{control.inbound_node}"""
            conn.run(
                f"""CREATE SUBSCRIPTION {sub_name} CONNECTION '{control.connection_string}' PUBLICATION trktr_pub_66fbce89_1855_4593_91f0_f9047edd1306 WITH (copy_data = false, enabled = true, origin = none, disable_on_error = true);""")
            return Response(status_code=201)
        elif (request.method == """DELETE"""):
            sql = f"""DROP SUBSCRIPTION trktr_sub_{NODE}_{control.inbound_node};"""
            conn.run(sql)
    except Exception as e:
        return Response(status_code=500, content=dumps({"""error""": str(e)}), media_type="""application/json""")
    finally:
        if conn:
            conn.close()

    return Response(status_code=201)


@app.patch(COMMON_PATH_V1 + """/replicaset""", tags=["""replicaset_commit"""])
async def repset(request: Request, api_key: APIKey = Depends(api_key_auth)):
    """COMMIT a local replicaset."""
    try:
        conn = conn = get_pg_connection(
            database=DB_CONN_INFO.dbname, host=DB_CONN_INFO.host, port=DB_CONN_INFO.port, user=DB_CONN_INFO.user, password=DB_CONN_INFO.password, sslcert=DB_CONN_INFO.sslcert, sslkey=DB_CONN_INFO.sslkey, sslrootcert=DB_CONN_INFO.sslrootcert)
        conn.run(
            """CALL trktr.trktr_commit_replicaset()""")
    except Exception as e:
        return Response(status_code=500, content=dumps({"""error""": str(e)}), media_type="""application/json""")
    finally:
        if conn:
            conn.close()

    return Response(status_code=200)


@app.put(COMMON_PATH_V1 + """/replicaset/{table}""", tags=["""replicaset_add_table"""])
@app.delete(COMMON_PATH_V1 + """/replicaset/{table}""", tags=["""replicaset_remove_table"""])
async def repset_table(table: str, request: Request, api_key: APIKey = Depends(api_key_auth)):
    """Add or remove a TABLE to/from the local replicaset."""
    try:
        conn = conn = get_pg_connection(
            database=DB_CONN_INFO.dbname, host=DB_CONN_INFO.host, port=DB_CONN_INFO.port, user=DB_CONN_INFO.user, password=DB_CONN_INFO.password, sslcert=DB_CONN_INFO.sslcert, sslkey=DB_CONN_INFO.sslkey, sslrootcert=DB_CONN_INFO.sslrootcert)
        parts = table.split('.')
        if len(parts) != 2:
            raise HTTPException(
                status_code=400,
                detail="""Malformed table expression""",
            )
        if (request.method == """PUT"""):
            try:
                conn.run(
                    """CALL trktr.trktr_add_table_to_replicaset(:p0, :p1)""", p0=parts[0], p1=parts[1])
            except Exception as e:
                return Response(status_code=409, content=dumps({"""error""": str(e)}), media_type="""application/json""")
            return Response(status_code=201)
        elif (request.method == """DELETE"""):
            try:
                conn.run(
                    """CALL trktr.trktr_remove_table_from_replicaset(:p0, :p1)""", p0=parts[0], p1=parts[1])
            except Exception as e:
                return Response(status_code=409, content=dumps({"""error""": str(e)}), media_type="""application/json""")
    except Exception as e:
        logger.error(e)
    finally:
        if conn:
            conn.close()

    return Response(status_code=200)

if __name__ == """__main__""":
    """Start houskeeping threads and serve the API."""
    if not is_server_compatible():
        exit(-1)

    if LSN_RESOLVER in ("""file_fdw""", """log_fdw"""):
        ct = threading.Thread(target=resolver_thread_function, args=())
        ct.start()

    wt = threading.Thread(target=sub_watcher_thread_function, args=())
    wt.start()

    if SSL_CERTFILE and SSL_KEYFILE:
        uvicorn.run("""arbiter:app""", host=API_HOST, port=int(API_PORT), reload=False,
                    ssl_keyfile=SSL_KEYFILE, ssl_certfile=SSL_CERTFILE)
    else:
        uvicorn.run("""arbiter:app""", host=API_HOST,
                    port=int(API_PORT), reload=False)
