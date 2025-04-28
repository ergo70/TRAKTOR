# TRAKTOR
TRAKTOR: Set up true multimaster replication clusters with vanilla PostgreSQL servers.

There is no user's guide here yet, but take a look at the [tutorial](https://github.com/ergo70/TRAKTOR/blob/main/doc/TRAKTOR_tutorial.md).

## This is TRAKTOR 2
[psycopg2](https://pypi.org/project/psycopg2/) has been replaced by [pg8000](https://pypi.org/project/pg8000/). No more native dependencies should make it way simpler to run this in limited environments, e.g. on AWS Lambda.
Compatibility with PostgreSQL < 16.x was removed for operational simplicity and security. Compatible with PostgreSQL 16 and 17.

## Caveats
TRAKTOR now requires active SSL on PostgreSQL connections.
After un-initializing a Node with DELETE /v1/arbiter/control, you must GRANT SUPERUSER to your TRAKTOR database user again, and DROP the EVENT TRIGGERS pgc_evtrg_trktr_protect_drop_replicaset and pgc_evtrg_trktr_protect_alter_replicaset manually. Otherwise a subsequent initialize will fail. 
