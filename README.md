# Protocol Proxy IEEE 2030.5 Library

This library provides an IEEE 2030.5 (Smart Energy Profile 2.0, CSIP) **client** to a
[Protocol Proxy](https://github.com/eclipse-volttron/lib-protocol-proxy) Manager. Communication happens in a separate
proxy process which acts as the EndDevice (DER client) towards one or more 2030.5 servers: it registers the device,
follows the server's DER programs, applies their controls as events, answers with DERControlResponses, and mirrors the
device's status, settings, capability and meter readings upward. One proxy process serves any number of servers.

## Automatically installed dependencies
- python = ">=3.11,<4.0"
- protocol-proxy = ">=2.0.0rc3"
- httpx (HTTPS client), xsdata (XML runtime for the generated `sep.xsd` models), cryptography (certificate
  fingerprints for the LFDI/SFDI identifiers).

# Installation

```shell
pip install protocol-proxy-ieee2030_5
```

This is rarely necessary: the library is a dependency of an application acting as a Protocol Proxy Manager, such as the
VOLTTRON IEEE 2030.5 Driver Interface (`volttron-lib-ieee2030_5-driver`), and is installed with it.

# How it works

A Protocol Proxy Manager launches the proxy with:

```shell
python -m protocol_proxy.proxy protocol_proxy.protocol.ieee2030_5.ieee2030_5_proxy:Ieee2030_5Proxy [manager options]
```

The proxy takes no protocol-specific launch options. Everything about a server arrives in messages, and every point is
identified by the topic the caller registered it under. The caller's remote id (protocol-proxy header version 2)
identifies the server in every message after `REGISTER_SERVER`:

| Message | Purpose |
|---|---|
| `REGISTER_SERVER` | Create (or re-register) the client for a server and replace its point table. Carries `server_url`, the TLS material (`cert_path`, `key_path`, `ca_path`, `tls_verify`, `tls12_only`, `ciphers`), the identity (`lfdi`, derived from the certificate when absent; `pin`; `register_if_missing`; `device_category`; `der_index`), the polling and subscription settings (`poll_rate_floor`, `default_poll_rate`, `poll_rate_ceiling`, `subscribe`, `notify_host`, `notify_port`, `notify_bind_host`, `notify_client_auth`, `notify_cert_path`, `notify_key_path`), `response_timeout`, `dcap_path` (default `/dcap`) and `points`: a list of `{topic, path, writable, multiplier, scaling, starting_value, reading}`. `wait` (seconds) makes the reply wait for the start-up phases; otherwise they run in the background. |
| `READ_RESOURCES` | Reply with the values of `topics`. Read-only rows (controls) come from the engine's current view; writable rows come from the shadow copies of what was last sent. `refresh: true` fetches the server's controls first. |
| `WRITE_RESOURCES` | Apply `values` (`{topic: value}`) to the upward resources: one PUT per touched DERStatus, DERSettings, DERCapability, DERAvailability or DeviceInformation, and one MirrorMeterReading POST per reading row. |
| `DESCRIBE_SERVER` | The session as discovered: EndDevice href, programs, DER, MirrorUsagePoint, subscriptions, start-up error. |
| `CLOSE_SERVER` | Delete the subscriptions, stop the notification receiver and forget the server. |

Replies have the form `{"result": ..., "error": ...}`. For reads `result` maps topic to value and `error` maps a topic
to its reason (`unregistered topic`, `session not ready`, or the start-up error). For writes `result` maps topic to
`{status, resource, value}` and `error` carries the HTTP failure or why nothing was sent.

## Point paths and the convention

A point's `path` names a 2030.5 resource and an attribute, written `DERSettings.setMaxW`, `DERSettings::setMaxW` or
`DERSettings/setMaxW`. The paths follow the interoperability service's `2030.5` convention so the service's transforms
apply unchanged:

* **Upward (writable) rows**: `DERStatus.<attr>`, `DERSettings.<attr>`, `DERCapability.<attr>`, `DERAvailability.<attr>`,
  `DeviceInformation.<attr>` and `MirrorMeterReading.<unit>` (`W`, `var`, `VA`, `Hz`, `A`, `PF`, and `V.PhaseA` ...
  `V.PhaseCA`). Quantities are one engineering number; the proxy wraps them as `{value, multiplier}` using the row's
  `multiplier` (the power of ten the wire value is expressed in). Status structs (`operationalModeStatus`,
  `stateOfChargeStatus`...) take the enumeration value and are stamped with the server time; `connectStatus` (an alias
  of the schema's `genConnectStatus`) is a hex bitmap. Hex-binary attributes (`modesEnabled`, `alarmStatus`) take an
  integer or a hex string.
* **Downward (read-only) rows**: `DERControl.<attr>` for the control in force (the active events overlaid on the
  program's DefaultDERControl), `DefaultDERControl.<attr>`, `DERCurve.<curve attribute>.<attr>` (for example
  `DERCurve.opModVoltVar.CurveData`, a list of `{xvalue, yvalue}`), `DERControlList` (one list-valued point of every
  scheduled and active event: `{mRID, status, interval: {start, duration}, primacy, DERControl: {...}, DERCurve: {...}}`)
  and `DERProgram.<attr>` of the program in charge.

A row's `scaling` multiplies values read and divides values written (for example `0.001` to present `opModTargetW` in
kW). `starting_value` seeds the shadow of an upward resource so required attributes (`DERCapability.type`,
`DERCapability.rtgMaxW`, `DERSettings.setGradW`...) are present from the first PUT; any still missing are sent as zero
with a warning. `reading` carries the ReadingType of a `MirrorMeterReading` row (`uom`, `phase`, `kind`,
`flowDirection`, `dataQualifier`, `accumulationBehaviour`, `commodity`, `powerOfTenMultiplier`, `mrid`), defaulted from
the unit and phase in the path.

## The client flow

On registration the proxy runs the CSIP start-up phases in the background: `GET /dcap`, time synchronisation against
the server's `Time` resource (server time is used for every timestamp), EndDevice lookup by LFDI (or registration with
`POST EndDevice` when `register_if_missing`), the `Registration` PIN check, FunctionSetAssignments and their
DERPrograms sorted by primacy, the DER with its capability, settings, status and availability links, and the creation
of a MirrorUsagePoint with one MirrorMeterReading per reading row.

Controls are then fetched from every program's DefaultDERControl and DERControlList at the DERProgramList's `pollRate`
(bounded by `poll_rate_floor` and `poll_rate_ceiling`, `default_poll_rate` when the server gives none). When the
EndDevice's DERList is empty the proxy declares its DER with a PUT (`create_der_if_missing`), as some servers expect. Each DERControl is an event: the proxy
draws its `randomizeStart` and `randomizeDuration` once, moves it from scheduled to active to complete on the server
clock, honours cancellations and supersessions announced by the server, and resolves overlapping active events that set
the same attribute by program primacy (lower wins) and then creation time (later wins). Every transition the event asks
to hear about (`responseRequired` bits) is answered with a DERControlResponse to its `replyTo`: received (1), started
(2), completed (3), cancelled (6), superseded (7), rejected as invalid (253) or expired (254).

Whenever the view of the controls changes, by poll, notification or a scheduled transition, the proxy sends the changed
topics to the manager as `RECEIVE_CONTROLS` with the payload `{"result": {topic: value, ...}, "error": {}}`, scaled
like a read. Only registered topics are forwarded.

## Server role

A registration whose `role` is `server` makes the proxy *serve* a CSIP server to DER clients instead of reaching one: an
HTTPS server on `bind_host:port` (the server certificate in `cert_path`/`key_path`, client certificates verified against
`ca_path` when `client_auth` is on; `tls: false` serves plain HTTP for testing) with the resource tree a client expects.
`/dcap` links Time, the EndDeviceList, one DERProgramList and the MirrorUsagePointList; `/derp/1` is the program (its
`primacy` from the registration) with `/derp/1/dderc` (DefaultDERControl), `/derp/1/derc` (DERControlList) and
`/derp/1/dc` (DERCurveList); each DER client gets an EndDevice with Registration (PIN), FunctionSetAssignments, a DER
with the four upward links, DeviceInformation and a SubscriptionList. `client_lfdi`/`client_pin` pre-register the DER
whose data maps to the registered points (otherwise the first client to register is that DER);
`register_unknown_clients` lets other clients create EndDevices.

The point rows are the same convention paths with the directions mirrored. The platform writes the controls:
`DefaultDERControl.*`; `DERControl.*`, which creates one *immediate* event per write request (start now, duration
`immediate_control_duration`, status active) superseding the previous one; `DERControlList`, the schedule, written as a
list of `{mRID?, interval: {start, duration}, DERControl: {...}}` entries (events that disappear are cancelled, finished
events are dropped later); `DERCurve.<attribute>.*` (a DERCurve per curve attribute, linked from controls that set that
attribute to true or to its href); and `DERProgram.*`. The DER client's PUTs of DERStatus, DERSettings, DERCapability,
DERAvailability and DeviceInformation, its MirrorMeterReadings (matched by mRID, else by unit and phase) and its
DERControlResponses are flattened into the convention and pushed to the caller as `RECEIVE_CONTROLS`, scaled per row.
`READ_RESOURCES` reports what the server holds for either kind of row; `DERControlList` reads back as the events with
their status and the responses received. Clients subscribed to the control resources are notified (`POST` of a
`Notification` to their URI) whenever a platform write changes them.

The served state (registrations, PINs, subscriptions, controls, curves, received values, responses) is written to
`<state_dir>/sep2_server_<host>_<port>.json` on every change and restored when the proxy starts again, so a proxy
restart leaves clients registered and subscribed. Registration `values` (the caller's current values) seed served
points that hold nothing when there is no state to restore.

## Subscriptions and notifications

With `subscribe` true and a `notify_host` the proxy listens for notifications on `notify_bind_host:notify_port` (TLS
with the client certificate, or with `notify_cert_path`/`notify_key_path` when the server's notifier wants a server-style
certificate; plain HTTP when there is none) and subscribes to each program's DERControlList and
DefaultDERControl and to the FunctionSetAssignmentsList, advertising `https://<notify_host>:<notify_port>/notify`. A
server that refuses subscriptions (no SubscriptionList, or 4xx on POST) leaves the client on polling; polling continues
as a backstop even when subscriptions are live. A notification refreshes the controls at once; a notification about the
FSA list, or one announcing a moved or deleted resource, reloads the programs as well.

## TLS

IEEE 2030.5 requires TLS 1.2 with `TLS_ECDHE_ECDSA_WITH_AES_128_CCM_8`. The proxy offers that suite first and
`ECDHE-ECDSA-AES128-GCM-SHA256` second (`ciphers` overrides the list), pins TLS 1.2 (`tls12_only`, since a TLS 1.3
handshake ignores the cipher list), authenticates with `cert_path`/`key_path`, and verifies the server against
`ca_path` or the system store unless `tls_verify` is false. The device's LFDI is the SHA-256 fingerprint of the
certificate truncated to 160 bits; the SFDI follows from it with a check digit.

## Models

`models/sep.py` is the generated dataclass rendering of the IEEE 2030.5-2018 schema, vendored from the GridAPPS-D
2030.5 server distribution (see `models/SOURCE.md` for provenance and licence). `models/xml.py` serialises and parses
it with xsdata, leniently so vendor extensions do not break parsing; `models/convert.py` is the boundary between the
flat convention and the schema's quantities, status structs and hex-binary types.

# Development

```shell
pip install -e .
python -m pytest tests
```

The tests run against an in-process fake CSIP server behind `httpx.MockTransport` and a real TLS loopback socket for
the notification receiver; nothing listens beyond the loopback interface. The end-to-end harness against the GridAPPS-D
Go `sep2server` in Docker lives with the driver interface (`volttron-lib-ieee2030_5-driver`, `tests/e2e_sep2server/`),
since it drives the whole stack from the interface down.
