# Per-script execution timeout

The worker defaults to `job_timeout_seconds` from its config (1200 seconds / 20 minutes if omitted).

A signed Python script can request its own timeout by adding the following key to its existing literal `SCRIPT_SCHEMA_JSON` object:

```python
SCRIPT_SCHEMA_JSON = r'''
{
  "executionTimeoutSeconds": 86400,
  "fields": []
}
'''
```

The value must be an integer between 1 and 86400 seconds (24 hours). It is metadata, not a form field. No server or Remote Work Manager changes are required. The worker statically parses the Python script using `ast` and never executes it to read the timeout. Scripts without this key retain the configured default.
