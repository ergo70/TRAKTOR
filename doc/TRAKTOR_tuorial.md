# TRAKTOR Tutorial
TRAKTOR is a true multimaster replication solution for PostgreSQL on top of logical replication, implemented as a shared-nothing architecture.
It has been tested with 15.x and 16.x.
In this tutorial, you will initially setup a two node cluster and then extend it to three nodes.

## Preparing PostgreSQL

TRAKTOR uses the logical replication feature of PostgreSQL for true multimaster replication of data. So, all PostgreSQL servers participating in a cluster must be configured accordingly.

### General
postgresql.conf must contain the following settings:

```
wal_level=logical
log_destination = 'jsonlog'
logging_collector = on
log_file_mode = 0640
log_min_messages = info
lc_messages = 'C'
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

`CREATE USER traktor_arbiter PASSWORD 'traktor' LOGIN SUPERUSER;`

Unfortunately, PostgreSQL requires a SUPERUSER in order to control logical replication. Beyond this tutorial, more security than a Password is advisable.

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

We add another PostgreSQL 16.x server on ports 5435, so change the `port` entry in postgresql.conf accordingly.

Create the databases:

`CREATE DATABASE traktor_tutorial;`

Connect to each new database and create a schema:

`CREATE SCHEMA multimaster;`

Create the replication user:

`CREATE USER traktor_arbiter PASSWORD 'traktor' LOGIN SUPERUSER;`

And the multimaster.reptest Table:

```
CREATE TABLE multimaster.reptest (
	id int NOT NULL,
	payload text NULL,
	CONSTRAINT reptest_pk PRIMARY KEY (id)
);
```

Now, on the arbiter node:

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

```
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

And test it.

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

And that's it. A fully functional three node multimaster replicating cluster with vanilla PostgreSQL servers.
