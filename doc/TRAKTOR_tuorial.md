# TRAKTOR Tutorial
TRAKTOR is a true multimaster replication solution for vanilla PostgreSQL on top of logical replication, implemented as a shared-nothing architecture.
It has been tested with 15.x and 16.x.
In this tutorial, you will initially setup a two node cluster and then extend it to three nodes.

## Preparing PostgreSQL

TRAKTOR uses the logical replication feature of PostgreSQL for true multimaster replication of data. So, all PostgreSQL servers participating in a cluster must be configured accordingly.

### General
postgresql.conf must contain the following settings:

```
wal_level=logical
log_destination = 'jsonlog' # You can add others, but jsonlog has to be available
logging_collector = on
log_file_mode = 0640
log_min_messages = error # At least error is required
```

### Addressing the database servers
Each node must be distinguishable by address. If you test on a single machine, it is sufficient to have separate data directories and set different ports in postgresql.conf, e.g.:

```
port=5433
port=5434
port=5435
```

## Installing the Arbiter nodes
The Arbiter nodes work alongside each PostgreSQL server and provide all necessary functionality for multimaster replication. The are configured by configuration file, and controlled via a REST API.

Setting up the arbiter nodes is straightforward:

1. Install Python3, Development and testing were done with 3.10.x
1. Install packages from requirements.txt
1. Create directories for each node you want to run
1. Copy arbiter.py, arbiter.ini and logging.conf into each directory
1. Change arbiter.ini to match your setup

requirements.txt references psycopg2-binary by default, the PostgreSQL driver including all necessary native binaries. If you have them already installed, you can install psycopg instead.

You will also need tools to connect to the database, e.g. psql or [dBeaver](https://dbeaver.io/) CE, and to make HTTP/S calls, e.g. curl or [Postman](https://www.postman.com/).

```
[DEFAULT]
NodeID = # The individual node id. This is an Integer
ConnectionString = # The PostgreSQL [keyword/value connection string](https://www.postgresql.org/docs/current/libpq-connect.html#LIBPQ-CONNSTRING-KEYWORD-VALUE), e.g. host=127.0.0.1 port=5433 user=<user> password=<password> dbname=tutorial
APIAddress = 127.0.0.1:8080 # The API Endpoint
CheckInterval = 10 # How often to check for conflicts, in Seconds
APIKey =  # The secret API Key
AutoHeal = True # Enable automatic conflict resolution
Pre16Compatibility = False # If you run 16.x server mixed in a cluster with pre-16.x servers, this must be True, since < 16.x uses a different cycle resolution method, and >= 16.x has to emulate this
```

## Let's go
We start with two PostgreSQL 16.x servers on ports 5433 and 5434, so change the `port` entry in postgresql.conf accordingly.

Create the databases:

`CREATE DATABASE traktor_tutorial;`

Connect to each new database and create a schema:

`CREATE SCHEMA multimaster;`

Create the replication user:

Unfortunately, PostgreSQL < 16.x requires SUPERUSER privilege in order to create logical replication subscriptions.
Since 16.x, this is not required anymore. According to the [documentation](https://www.postgresql.org/docs/16/logical-replication-security.html), membership in pg_create_subscription is sufficient, but SUPERUSER will still work.

**So for the sake of simplicity, you might just continue with SUPERUSER.**

before 16.x: `CREATE USER traktor_arbiter PASSWORD 'traktor' LOGIN SUPERUSER;`

since 16.x:
```
CREATE USER traktor_arbiter PASSWORD 'traktor' LOGIN REPLICATION;
GRANT CREATE ON DATABASE traktor_tutorial TO traktor_arbiter;
GRANT pg_create_subscription TO traktor_arbiter;
GRANT EXECUTE ON FUNCTION pg_catalog.pg_current_logfile(text) TO traktor_arbiter;
GRANT SELECT ON pg_subscription TO traktor_arbiter;
```

Still, the user runs with elevated privileges, so beyond this tutorial, more security than a Password is advisable.

Now, on the arbiter nodes:

### Node 0
```
[DEFAULT]
NodeID = 0
ConnectionString = host=127.0.0.1 port=5433 user=traktor_arbiter password=traktor dbname=traktor_tutorial
APIAddress = 127.0.0.1:8080
CheckInterval = 10
APIKey =  LetMeIn
AutoHeal = True
Pre16Compatibility = False
```

### Node 1

```
[DEFAULT]
NodeID = 1
ConnectionString = host=127.0.0.1 port=5434 user=traktor_arbiter password=traktor dbname=traktor_tutorial
APIAddress = 127.0.0.1:8081
CheckInterval = 10
APIKey =  LetMeIn
AutoHeal = True
Pre16Compatibility = False
```

### Starting the Arbiter

Run them:

`python3 arbiter.py` for each directory you installed it in.

You should see output like this:
```
INFO:     Started server process [164]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on https://127.0.0.1:8080 (Press CTRL+C to quit)
2023-10-09 11:30:50,123 loglevel=INFO   logger=__main__ check_failed_subscriptions() L416  No FAILed subscriptions found
2023-10-09 11:30:50,199 loglevel=WARNING logger=__main__ resolve_conflicts() L502  relation "trktr.history" does not exist
LINE 1: SELECT lsn, "subscription" FROM trktr.history WHERE resolved...
```

The WARNING is ok for now, because the node has not been initialized yet. So let's fix this.

### Initialize the Database via API

For the next steps, you will use the API. The OpenAPI documentation can be found here:
```
http://localhost:8080/docs
http://localhost:8081/docs
```

To initialize the database to participate in a TRAKTOR cluster, you do a PUT on /v1/arbiter/control. In our case:

```
curl --location --request PUT 'http://localhost:8080/v1/arbiter/control' \
--header 'X-API-KEY: LetMeIn'
```
201 Created

```
curl --location --request PUT 'http://localhost:8081/v1/arbiter/control' \
--header 'X-API-KEY: LetMeIn'
```
201 Created

Now, the WARNING about missing database objects should be gone.

You also need mutual SUBSCRIPTIONS. Again, those are created by API call:

`inbound_node` is the node ID the subscription points to, `connection_string` the matching connection string for the respective database server.

```
curl --location --request PUT 'http://localhost:8080/v1/arbiter/subscription/control' \
--header 'Content-Type: application/json' \
--header 'X-API-KEY: LetMeIn' \
--data '{"inbound_node": 1, "connection_string": "host=localhost dbname=traktor_tutorial port=5434 user=traktor_arbiter password=traktor"}'
```
201 Created

```
curl --location --request PUT 'http://localhost:8081/v1/arbiter/subscription/control' \
--header 'Content-Type: application/json' \
--header 'X-API-KEY: LetMeIn' \
--data '{"inbound_node": 0, "connection_string": "host=localhost dbname=traktor_tutorial port=5433 user=traktor_arbiter password=traktor"}'
```
201 Created

Again, the use of cleartext passwords in the connection_string is NOT recommended for production setups!

`SELECT subname FROM pg_subscription;` should show trktr_sub_0_1 on Node 0 and trktr_sub_1_0 on Node 1 now.

### Create a TABLE for replication
In both database servers, create a simple test table:

```
CREATE TABLE multimaster.reptest (
	id int NOT NULL,
	payload text NULL,
	CONSTRAINT reptest_pk PRIMARY KEY (id)
);
```

### Managing the Replicaset
To add or remove tables from replication, they first must be added or removed from the Replicaset via API:
```
curl --location --request PUT 'http://localhost:8080/v1/arbiter/replicaset/multimaster.reptest' \
--header 'X-API-KEY: LetMeIn'
```
201 Created

```
curl --location --request PUT 'http://localhost:8081/v1/arbiter/replicaset/multimaster.reptest' \
--header 'X-API-KEY: LetMeIn'
```
201 Created

The TABLE multimaster.reptest is now scheduled for replication, but not active yet. To process all pending add/remove operations in the Replicaset, it must be COMMITTed on both nodes.

```
curl --location --request PATCH 'http://localhost:8080/v1/arbiter/replicaset' \
--header 'X-API-KEY: LetMeIn'
```
200 OK

```
curl --location --request PATCH 'http://localhost:8081/v1/arbiter/replicaset' \
--header 'X-API-KEY: LetMeIn'
```
200 OK

The status of the Replicaset can be queried by:
```
curl --location 'http://localhost:8081/v1/arbiter/replicaset/status' \
--header 'X-API-KEY: LetMeIn'
```

Now, replication is all set.

### Testing multimaster replication

On Node 0:

`INSERT INTO multimaster.reptest (id, payload) VALUES (0, 'Hello');`

On Node 1:

`INSERT INTO multimaster.reptest (id, payload) VALUES (1, 'TRAKTOR');`

On both Nodes:

`SELECT * FROM multimaster.reptest ORDER BY id ASC;`

should now show:

|id|payload|
|--|-------|
|1|Hello|
|2|TRAKTOR|

Congratulations! You have just set up your first multimaster replication cluster with TRAKTOR.

## Extending the cluster

We add another PostgreSQL  server on ports 5435, so change the `port` entry in postgresql.conf accordingly.

Create the databases:

`CREATE DATABASE traktor_tutorial;`

Connect to each new database and create a schema:

`CREATE SCHEMA multimaster;`

Create the replication user:

before 16.x: `CREATE USER traktor_arbiter PASSWORD 'traktor' LOGIN SUPERUSER;`

since 16.x:
```
CREATE USER traktor_arbiter PASSWORD 'traktor' LOGIN REPLICATION;
GRANT CREATE ON DATABASE traktor_tutorial TO traktor_arbiter;
GRANT pg_create_subscription TO traktor_arbiter;
GRANT EXECUTE ON FUNCTION pg_catalog.pg_current_logfile(text) TO traktor_arbiter;
GRANT SELECT ON pg_subscription TO traktor_arbiter;
```

And the multimaster.reptest Table:

```
CREATE TABLE multimaster.reptest (
	id int NOT NULL,
	payload text NULL,
	CONSTRAINT reptest_pk PRIMARY KEY (id)
);
```

Next, on the arbiter node:

### Node 2
Create a new directory as shown above, with the following arbiter.ini

```
[DEFAULT]
NodeID = 2
ConnectionString = host=127.0.0.1 port=5435 user=traktor_arbiter password=traktor dbname=traktor_tutorial
APIAddress = 127.0.0.1:8082
CheckInterval = 10
APIKey =  LetMeIn
AutoHeal = True
Pre16Compatibility = False
```

and start arbiter.py.

Now, all nodes must be included in the new three node cluster:

Initialize node 2:
```
curl --location --request PUT 'http://localhost:8082/v1/arbiter/control' \
--header 'X-API-KEY: LetMeIn'
```
201 Created

```
curl --location --request PUT 'http://localhost:8082/v1/arbiter/subscription/control' \
--header 'Content-Type: application/json' \
--header 'X-API-KEY: LetMeIn' \
--data '{"inbound_node": 1, "connection_string": "host=localhost dbname=traktor_tutorial port=5434 user=traktor_arbiter password=traktor"}'
```
201 Created

```
curl --location --request PUT 'http://localhost:8082/v1/arbiter/subscription/control' \
--header 'Content-Type: application/json' \
--header 'X-API-KEY: LetMeIn' \
--data '{"inbound_node": 0, "connection_string": "host=localhost dbname=traktor_tutorial port=5433 user=traktor_arbiter password=traktor"}'
```
201 Created

On node 0:

```
curl --location --request PUT 'http://localhost:8080/v1/arbiter/subscription/control' \
--header 'Content-Type: application/json' \
--header 'X-API-KEY: LetMeIn' \
--data '{"inbound_node": 2, "connection_string": "host=localhost dbname=traktor_tutorial port=5435 user=traktor_arbiter password=traktor"}'
```
201 Created

On node 1:

```
curl --location --request PUT 'http://localhost:8081/v1/arbiter/subscription/control' \
--header 'Content-Type: application/json' \
--header 'X-API-KEY: LetMeIn' \
--data '{"inbound_node": 2, "connection_string": "host=localhost dbname=traktor_tutorial port=5435 user=traktor_arbiter password=traktor"}'
```
201 Created

Add multimaster.reptest to the Replicaset on node 2:

```
curl --location --request PUT 'http://localhost:8082/v1/arbiter/replicaset/multimaster.reptest' \
--header 'X-API-KEY: LetMeIn'
```
201 Created

And COMMIT:

```
curl --location --request PATCH 'http://localhost:8082/v1/arbiter/replicaset' \
--header 'X-API-KEY: LetMeIn'
```
200 OK

Test it.

On node 2:

`INSERT INTO multimaster.reptest (id, payload) VALUES (3, 'Hello, Node 2');`

On node 0 or 1:

`INSERT INTO multimaster.reptest (id, payload) VALUES (4, 'Hello again, Node 2');`

On node 0 and 1 `SELECT * FROM multimaster.reptest ORDER BY id ASC;` should now show:

|id|payload|
|--|-------|
|1|Hello|
|2|TRAKTOR|
|3|Hello, Node 2|
|4|Hello again, Node 2|

And on your new node 2:

|id|payload|
|--|-------|
|3|Hello, Node 2|
|4|Hello again, Node 2|

And that's it. A fully functional three node multimaster replicating cluster with vanilla PostgreSQL servers. The hard work of designing a conflict free schema begins now. Stay tuned.

## Automatic conflict resolution

In case of conflicting keys in replicated TABLEs, PostgreSQL will stop the replication.
Usually, this has to be fixed manually, e.g. as described [here](https://www.postgresql.fastware.com/blog/addressing-replication-conflicts-using-alter-subscription-skip). TRAKTOR can do this automatically if `AutoHeal` is activated. Normally, such conflicts are rare, but can occur if the cluster experienced a split-brain situation, i.e. not all nodes could communicate with each other due to network issues. Resolution by skipping LSN is a "Local Write Wins" strategy.

Let's try:

1. Stop the PostgreSQL server for node 0. The Arbiters can keep running
1. On node 1: `INSERT INTO multimaster.reptest (id, payload) VALUES (5, 'Oh no!');`
1. Stop node 1
1. Start node 0
1. On node 0: `INSERT INTO multimaster.reptest (id, payload) VALUES (5, 'A conflict!');`
1. Start node 1

This was the split-brain. Node 0 and 1 now have conflicting rows for the same key. The replication stops immediately but the Arbiter nodes notice this, automatically apply the necessary `ALTER SUBSCRIPTION SKIP` commands, and re-ENABLE the affected SUBSCRIPTIONs.
Better yet, they also write the cause of the conflict(s) into trktr.history. Because the conflict was solved purely technical, you might want to look up the offending entries in trktr.history and fix the state of the cluster on the logical level. But that is optional.

trktr.history on each node now will contain a row like this:

|subscription|occurred|lsn|relation|key|value|resolved|
|------------|--------|---|--------|---|-----|--------|
|trktr_sub_0_1|2023-10-11 11:15:15.099|0/28F6BC8|multimaster.reptest|id|5|2023-10-11 11:15:18.291776|

Cool, ain't it?

Since the necessary information has to be parsed out of the PostgreSQL server logfile, TRAKTOR uses a language agnostic parser. It should work with all languages, but only English and German have been tested.

### Removing tables from the Replicaset

```
curl --location --request DELETE 'http://localhost:8080/v1/arbiter/replicaset/multimaster.reptest' \
--header 'X-API-KEY: LetMeIn'
```
200 OK

```
curl --location --request DELETE 'http://localhost:8081/v1/arbiter/replicaset/multimaster.reptest' \
--header 'X-API-KEY: LetMeIn'
```
200 OK

```
curl --location --request DELETE 'http://localhost:8082/v1/arbiter/replicaset/multimaster.reptest' \
--header 'X-API-KEY: LetMeIn'
```
200 OK
```
curl --location --request PATCH 'http://localhost:8080/v1/arbiter/replicaset' \
--header 'X-API-KEY: LetMeIn'
```
200 OK
```
curl --location --request PATCH 'http://localhost:8081/v1/arbiter/replicaset' \
--header 'X-API-KEY: LetMeIn'
```
200 OK
```
curl --location --request PATCH 'http://localhost:8082/v1/arbiter/replicaset' \
--header 'X-API-KEY: LetMeIn'
```
200 OK

The table multimaster.reptest is now removed from replication. You can INSERT the same key on all nodes without collision or DROP the tables now.

## Monitoring TRAKTOR nodes

Every TRAKTOR arbiter node has three API calls to monitor its status. To see the status of the node itself, call
```
curl --location 'http://localhost:8080/v1/arbiter/status' \
--header 'X-API-KEY: LetMeIn'
```
200 OK

```
{
    "node": 0,
    "replicating": true,
    "tainted": true,
    "auto_resolved": 1,
    "replication_lag_ms": 0.139,
    "server_version": 16.0,
    "pre_16_compatibility": false
}
```

The meaning of those fields is a follows:

1. node: The Node ID
1. replicating: Is the Node currently replicating ok, i.e. there are no failed SUBSCRIPTIONs
1. tainted: If the LSN Resolver has fixed a conflict, the mutual data of the TRAKTOR cluster could be in an inconsistent state. This node is tainted
1. auto_resolved: How many conflicts were auto resolved since the initialization of the TRAKTOR cluster
1. replication_lag_ms: The current [replication lag](https://www.percona.com/blog/replication-lag-in-postgresql) in milliseconds
1. server_version: The PostgreSQL version number
1. pre_16_compatiblity: Is the compatibility mode for PostgreSQL &lt; 16.x activated

To see the status of the node replicaset, call
```
curl --location 'http://localhost:8080/v1/arbiter/replicaset/status' \
--header 'X-API-KEY: LetMeIn'
```
200 OK
If you have removed the tables in the previous step, you will see:
```
{
    "node": 0,
    "replicaset": []
}
```
The replicaset is empty, otherwise you should see:
```
{
    "node": 0,
    "replicaset": [
        {
            "relation": "multimaster.reptest",
            "status": "active"
        }
    ]
}
```
The replicaset on Node 0 contains one TABLE, multimaster.reptest, which is currently in active replication. 

If the cluster is tainted and auto_resolved is > 0, you might want to see the resolution history to see which data might be inconsistent. Call:
```
curl --location 'http://localhost:8080/v1/arbiter/resolution/history' \
--header 'X-API-KEY: LetMeIn'
```
200 OK
```
{
    "node": 0,
    "resolutions": [
        {
            "occurred": "2023-11-03T22:32:15.509",
            "lsn": "0/3AED440",
            "relation": "multimaster.reptest",
            "sql_state_code": "23505",
            "resolved": "2023-11-03T22:43:28.841261",
            "subscription": "trktr_sub_0_1",
            "reason": "Key (id)=(3) already exists."
        }
    ]
}
```

1. node: The Node ID
1. resolutions: An array of resolution objects, or empty

Every Resolution object shows:

1. occurred: The timestamp of conflict detection
1. lsn: The logical sequence number of the conflict
1. relation: The relation involved in the conflict
1. sql_state_code: The [SQL error code](https://www.postgresql.org/docs/current/errcodes-appendix.html) of the conflict
1. resolved: The timestamp when the conflict was resolved. NULL if it has not been resolved yet
1. subscription: The subscription from which the conflict came from
1. reason: Details about the conflict

Please note, that _reason_ will be in the language determined by `LC_MESSAGES` in postgresql.conf.

