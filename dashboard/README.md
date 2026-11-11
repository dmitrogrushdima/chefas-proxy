# Dashboard

Static HTML, CSS and JavaScript served by `src/dashboard_server.py`.
There is no Node build step or browser-side mining logic.

Keep English, Russian and Simplified Chinese strings aligned in `i18n.js`.
Check JavaScript syntax with `node --check dashboard/app.js` and
`node --check dashboard/i18n.js` from the repository root.

Signing in as a farm administrator does not enable the private VPS view.
Manual fee controls require the unlocked private listener's `fee_test`
capability; the operator release keeps its fixed commission policy.
