# How to install the example dashboard in Grafana

This example dashboard works with the cluster built in the [tutorial](https://github.com/ergo70/TRAKTOR/blob/main/doc/TRAKTOR_tutorial.md). It assumes that Grafana is running on the same host than the TRAKTOR tutorial cluster. If not, you have to adapt Allowed Hosts of the datasource, and the URLs in the dashboard accordingly.

## Create a datasource

1. Install the [infinity-datasource](https://sriramajeyam.com/grafana-infinity-datasource/) plugin
1. Create a datasource named ```arbiter-node```
1. Authentication: ```API Key Value pair, Key: X-API-Key, Value: LetMeIn, AddTo: Header```
1. Network: ```Skip TLS Verify: ON```
1. Security: Allowed Hosts ```https://localhost, https://127.0.0.1, http://localhost, http://127.0.0.1```

## Import the Dashboard

Dashboards->Import->Traktor_Status_Overview.json
