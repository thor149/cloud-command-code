// Explicit entrypoint keeps `node --test test/` working on Node 22 on Windows.
import './auth.test.js';
import './commands.test.js';
import './api.test.js';
import './limits.test.js';
import './transports-cron.test.js';
