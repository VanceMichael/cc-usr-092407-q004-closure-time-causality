#!/bin/sh
set -eu

required_files="
contracts/disruption-event.schema.json
fixtures/airports.json
fixtures/flights.json
"

for path in $required_files; do
  if [ ! -s "$path" ]; then
    echo "Missing or empty scaffold input: $path" >&2
    exit 1
  fi
done

for field in event_id event_version event_type airport_code effective_from reported_at; do
  if ! grep -q "\"$field\"" contracts/disruption-event.schema.json; then
    echo "Event contract is missing field: $field" >&2
    exit 1
  fi
done

if ! grep -q '"timezone"' fixtures/airports.json; then
  echo "Airport fixture has no timezone data" >&2
  exit 1
fi

for field in flight_id origin destination scheduled_departure scheduled_arrival; do
  if ! grep -q "\"$field\"" fixtures/flights.json; then
    echo "Flight fixture is missing field: $field" >&2
    exit 1
  fi
done

echo "Scaffold contracts and fixtures are present."
