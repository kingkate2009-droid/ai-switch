# AI Switch v2.4.9

## Vendor Availability

- Probe the configured check-in/login page first, falling back to the API URL when no check-in URL is configured.
- Treat HTTP 4xx/5xx responses and error-page bodies served with HTTP 200 as unavailable.
- Retry transient network/TLS failures and limit bulk-check concurrency.
- Persist availability results and include vendor availability checks in health-check runs so results survive refreshes.
