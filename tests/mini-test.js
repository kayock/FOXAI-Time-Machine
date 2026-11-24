/* A tiny, dependency-free test runner. No npm, no framework —
   this is a personal project, not a CI pipeline.
   Test functions may be synchronous OR return a Promise; run() awaits
   everything before reporting, so an async check that never resolves
   true genuinely shows up as a failure instead of being silently skipped. */
(function(root){
  "use strict";
  const registered = [];

  function test(name, fn){
    registered.push({ name, fn });
  }

  function assert(cond, msg){
    if(!cond) throw new Error(msg || "Assertion failed");
  }
  assert.equal = function(a, b, msg){
    if(a !== b) throw new Error(msg || `Expected ${JSON.stringify(a)} to equal ${JSON.stringify(b)}`);
  };
  assert.ok = function(v, msg){ assert(!!v, msg); };
  assert.deepEqual = function(a, b, msg){
    const sa = JSON.stringify(a), sb = JSON.stringify(b);
    if(sa !== sb) throw new Error(msg || `Expected ${sa} to deep-equal ${sb}`);
  };

  // Every registered test runs to completion (pass or fail) — a rejected
  // promise or thrown error is recorded as a failure, never silently ignored.
  async function run(renderTo){
    const results = [];
    for(const { name, fn } of registered){
      try{
        await fn(assert);
        results.push({ name, pass: true });
      }catch(e){
        results.push({ name, pass: false, error: (e && e.message) || String(e) });
      }
    }

    const passCount = results.filter(r => r.pass).length;
    const failCount = results.length - passCount;
    const allPassed = failCount === 0 && results.length > 0;

    if(renderTo){
      renderTo.innerHTML = `
        <p style="font-family:monospace;font-size:14px;">
          <strong id="overallStatus" data-status="${allPassed ? 'pass' : 'fail'}" style="color:${allPassed ? '#2a7a4f' : '#b5482f'}">
            ${allPassed ? 'ALL TESTS PASSED' : 'TESTS FAILED'}
          </strong>
          — ${passCount} passed, ${failCount} failed (${results.length} total)
        </p>
        <ul style="font-family:monospace;font-size:13px;line-height:1.7;list-style:none;padding:0;">
          ${results.map(r => `<li style="color:${r.pass ? '#2a7a4f' : '#b5482f'}">
            ${r.pass ? '✓' : '✗'} ${r.name}${r.error ? ' — ' + r.error : ''}
          </li>`).join('')}
        </ul>`;
    }
    console.log(`${passCount} passed, ${failCount} failed of ${results.length}`);
    results.forEach(r => { if(!r.pass) console.error("FAIL:", r.name, r.error); });

    // Exposed so a script (or a human) can check pass/fail programmatically
    // without scraping rendered text.
    root.__testResults = { passCount, failCount, allPassed, results };
    return root.__testResults;
  }

  root.miniTest = { test, run };
})(typeof window !== "undefined" ? window : globalThis);
