# How to install the example dashboard in Grafana

This example dashboard works with the cluster built in the [tutorial](doc/TRAKTOR_tutorial.md).

## Create a datasource

1. Install the [infinity-datasource](https://sriramajeyam.com/grafana-infinity-datasource/) plugin
1. Create a datasource named arbiter-node
1. Authentication: ```API Key Value pair, Key: X-API-Key, Value: LetMeIn, AddTo: Header```
1. Network: ```Skip TLS Verify: ON```
1. Security: Allowed Hosts ```https://localhost, https://127.0.0.1, http://localhost, http://127.0.0.1```

## Import the Dashboard

Dashboards->Import->Traktor_Status_Overview.json
