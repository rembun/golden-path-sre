// Load script for the availability API (k6).
//
//   SCENARIO=steady  ~70 req/s, the normal day
//   SCENARIO=peak    ramps to ~1,000 req/s and holds (Thursday 09:00 booking launch)
//   SCENARIO=storm   steady traffic, then a short venue-capacity outage while clients
//                    retry aggressively (no backoff, Retry-After ignored), roughly
//                    tripling traffic. The outage is triggered by this script through
//                    the fake service's switch, so the incident is reproducible.
//
// Options (environment variables):
//   BASE_URL            default http://availability-api.bookings.svc (in-cluster)
//   CAPACITY_ADMIN_URL  default http://venue-capacity-fake.bookings.svc:8081
//   RATE_SCALE          multiplies every rate (e.g. 0.3 on a small laptop), default 1
//   PEAK_MINUTES        how long the peak holds, default 25
//   OUTAGE_SECONDS      length of the capacity outage in the storm, default 60
//
// Run in the cluster with:  make load SCENARIO=steady|peak|storm
import http from 'k6/http';
import { sleep } from 'k6';
import { Counter, Rate } from 'k6/metrics';

const BASE_URL = __ENV.BASE_URL || 'http://availability-api.bookings.svc';
const ADMIN_URL = __ENV.CAPACITY_ADMIN_URL || 'http://venue-capacity-fake.bookings.svc:8081';
const SCALE = Number(__ENV.RATE_SCALE || 1);
const PEAK_MINUTES = Number(__ENV.PEAK_MINUTES || 25);
const OUTAGE_SECONDS = Number(__ENV.OUTAGE_SECONDS || 60);
const SCENARIO = __ENV.SCENARIO || 'steady';

const rate = (n) => Math.max(1, Math.round(n * SCALE));

// The SLI, measured from the client side: good = not 5xx AND under 300 ms.
// Same definition as the SLO rules, so the load test and the alerts agree.
const sloGood = new Rate('slo_good_requests');
const retries = new Counter('client_retries');

const scenarios = {
  steady: {
    traffic: {
      executor: 'constant-arrival-rate',
      rate: rate(70), timeUnit: '1s', duration: '10m',
      preAllocatedVUs: 50, maxVUs: 300,
    },
  },
  peak: {
    traffic: {
      executor: 'ramping-arrival-rate',
      startRate: rate(70), timeUnit: '1s',
      preAllocatedVUs: 300, maxVUs: 2000,
      stages: [
        { target: rate(70), duration: '2m' },
        { target: rate(1000), duration: '1m' },            // bookings open at 09:00
        { target: rate(1000), duration: `${PEAK_MINUTES}m` },
        { target: rate(70), duration: '2m' },
      ],
    },
  },
  storm: {
    traffic: {
      executor: 'constant-arrival-rate',
      rate: rate(70), timeUnit: '1s', duration: '12m',
      preAllocatedVUs: 100, maxVUs: 1000,
      env: { RETRY_AGGRESSIVELY: '1' },
    },
    outage: {
      executor: 'shared-iterations',
      exec: 'capacityOutage',
      vus: 1, iterations: 1,
      startTime: '3m',                                     // 3 minutes of normal traffic first
      maxDuration: `${OUTAGE_SECONDS + 60}s`,
    },
  },
};

if (!scenarios[SCENARIO]) {
  throw new Error(`Unknown SCENARIO "${SCENARIO}" (use steady, peak or storm)`);
}

export const options = {
  scenarios: scenarios[SCENARIO],
  thresholds: {
    // Fails the run if the client-side SLI drops below the SLO target.
    // Expected to fail during the storm scenario: that is the incident.
    slo_good_requests: ['rate>=0.9985'],
  },
  summaryTrendStats: ['avg', 'p(95)', 'p(99)', 'max'],
};

function venue() {
  return `venue-${String(1 + Math.floor(Math.random() * 50)).padStart(3, '0')}`;
}

function dateWithinTwoWeeks() {
  const d = new Date(Date.now() + (1 + Math.floor(Math.random() * 14)) * 86400000);
  return d.toISOString().slice(0, 10);
}

function record(res) {
  sloGood.add(res.status > 0 && res.status < 500 && res.timings.duration < 300);
}

export default function () {
  const v = venue();
  const url = `${BASE_URL}/venues/${v}/availability?date=${dateWithinTwoWeeks()}`;
  let res = http.get(url, { tags: { name: 'availability' } });
  record(res);

  // The website and app retry immediately on failure, up to twice more, ignoring
  // Retry-After. During an outage this roughly triples the request rate.
  if (__ENV.RETRY_AGGRESSIVELY === '1') {
    for (let attempt = 0; attempt < 2 && res.status >= 500; attempt++) {
      retries.add(1);
      res = http.get(url, { tags: { name: 'availability_retry' } });
      record(res);
    }
  }

  // About 5% of customers go on to hold a slot.
  if (res.status === 200 && Math.random() < 0.05) {
    const slots = res.json('slots') || [];
    if (slots.length > 0) {
      const slot = slots[Math.floor(Math.random() * slots.length)];
      const hold = http.post(
        `${BASE_URL}/venues/${v}/holds`,
        JSON.stringify({ slot_id: slot.slot_id }),
        { headers: { 'Content-Type': 'application/json' }, tags: { name: 'hold' } },
      );
      record(hold);
    }
  }
}

// Storm scenario: flip the fake capacity service into an outage, then restore it.
export function capacityOutage() {
  const params = { headers: { 'Content-Type': 'application/json' } };
  console.warn(`capacity outage starts (${OUTAGE_SECONDS}s)`);
  http.put(`${ADMIN_URL}/admin/mode`, JSON.stringify({ mode: 'outage' }), params);
  sleep(OUTAGE_SECONDS);
  http.put(`${ADMIN_URL}/admin/mode`, JSON.stringify({ mode: 'normal' }), params);
  console.warn('capacity outage ends; watch whether the dependency recovers under retries');
}
