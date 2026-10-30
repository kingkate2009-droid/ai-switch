# AI Switch v2.4.9

## Vendor Availability

- Check the configured check-in/login page first, falling back to the API URL when no check-in URL is configured.
- Treat HTTP 4xx/5xx responses as unavailable and detect common HTML error pages returned with HTTP 200.
- Retry transient TLS, connection, and timeout failures once, and limit availability sweep concurrency to reduce spurious failures through local proxies/VPNs.
- Persist availability results and run vendor-level availability checks as part of health-check runs so results survive page refreshes.
- Bump the application version metadata to 2.4.9.
