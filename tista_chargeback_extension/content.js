// Tista Serverless Chargeback Engine & Dispute Closer
(function() {
  'use strict';

  const SEV_MAP = {
    'minor': 1.5,
    'low': 1.5,
    'medium': 2.0,
    'major': 2.5,
    'high': 2.5,
    'multiple': 4.0
  };

  const DELAY_TIERS = [
    { lo: 0, hi: 0.05, r: 0.2 },
    { lo: 0.05, hi: 0.15, r: 0.4 },
    { lo: 0.15, hi: 0.30, r: 0.6 },
    { lo: 0.30, hi: 0.50, r: 0.8 },
    { lo: 0.50, hi: Infinity, r: 1.0 }
  ];

  function getPrice(svc, cat, pix) {
    if (!window.TISTA_DEFAULT_CFG || !window.TISTA_DEFAULT_CFG.prices) return 0.21;
    const prices = window.TISTA_DEFAULT_CFG.prices;
    const item = prices.find(p => p[0] === svc && p[1] === cat && p[2] === pix);
    if (item) return item[3];
    const catItem = prices.find(p => p[0] === svc && p[1] === cat);
    if (catItem) return catItem[3];
    return 0.21;
  }

  function getServices() {
    if (!window.TISTA_DEFAULT_CFG) return ["Clipping path", "Image masking", "Drop shadow"];
    return [...new Set(window.TISTA_DEFAULT_CFG.prices.map(p => p[0]))];
  }

  function getCategories(svc) {
    if (!window.TISTA_DEFAULT_CFG) return ["Category 1", "Category 1.5", "Category 2"];
    return [...new Set(window.TISTA_DEFAULT_CFG.prices.filter(p => !svc || p[0] === svc).map(p => p[1]))];
  }

  // Direct Dispute Closer via Tista's own session
  async function closeDisputeDirect(disputeId, amount) {
    const editUrl = `/manage/vendors/1/order_disputes/${disputeId}/edit`;
    const resp = await fetch(editUrl);
    if (!resp.ok) throw new Error(`Could not access edit page (HTTP ${resp.status})`);
    const html = await resp.text();
    const parser = new DOMParser();
    const doc = parser.parseFromString(html, 'text/html');
    const token = doc.querySelector('input[name="authenticity_token"]')?.value ||
                  document.querySelector('meta[name="csrf-token"]')?.content;

    const formData = new URLSearchParams();
    formData.append('_method', 'patch');
    if (token) formData.append('authenticity_token', token);
    formData.append('order_dispute[chargeable_to]', 'vendor');
    formData.append('order_dispute[charge_to_vendor]', Math.max(2.0, parseFloat(amount) || 2.0).toFixed(2));
    formData.append('order_dispute[status]', 'closed');

    const postResp = await fetch(`/manage/vendors/1/order_disputes/${disputeId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: formData.toString()
    });

    if (postResp.ok || postResp.redirected) {
      return { success: true };
    }
    throw new Error(`Failed to update dispute (HTTP ${postResp.status})`);
  }

  // Check if we are on Dispute Edit Page
  if (window.location.pathname.includes('/order_disputes/') && window.location.pathname.endsWith('/edit')) {
    initEditPageHelper();
    return;
  }

  // Check if we are on Disputes List Page
  if (window.location.pathname.includes('/order_disputes')) {
    initDisputesListPage();
  }

  function initEditPageHelper() {
    const chargeInput = document.querySelector('#order_dispute_charge_to_vendor');
    const chargeableSelect = document.querySelector('#order_dispute_chargeable_to');
    const statusSelect = document.querySelector('#order_dispute_status');
    const form = document.querySelector('form.edit_order_dispute, form[action*="/order_disputes/"]');

    if (!chargeInput || !form) return;

    const bar = document.createElement('div');
    bar.className = 'tcb-bar';
    bar.innerHTML = `
      <div class="tcb-bar-title">⚡ Serverless Chargeback Quick-Fill</div>
      <div style="display:flex;gap:10px;align-items:center">
        <label style="font-size:12px;color:#8b949e">Amount:</label>
        <input type="number" id="tcb-quick-amt" value="2.00" step="0.01" min="2.00" style="width:75px;padding:4px 8px;border-radius:4px;border:1px solid #30363d;background:#161b22;color:#fff;font-family:monospace">
        <button type="button" id="tcb-btn-apply" class="tcb-btn">⚡ Set Vendor + $2.00 Min + Closed</button>
      </div>
    `;

    form.parentNode.insertBefore(bar, form);

    document.getElementById('tcb-btn-apply').addEventListener('click', () => {
      const amt = Math.max(2.0, parseFloat(document.getElementById('tcb-quick-amt').value) || 2.0);
      if (chargeableSelect) chargeableSelect.value = 'vendor';
      chargeInput.value = amt.toFixed(2);
      if (statusSelect) statusSelect.value = 'closed';
      bar.innerHTML = `<div class="tcb-bar-title" style="color:#2ecc71">✓ Set Chargeable: Vendor, Charge: $${amt.toFixed(2)}, Status: Closed! Click 'Update Order dispute' below to save.</div>`;
    });
  }

  function initDisputesListPage() {
    const table = document.querySelector('table');
    if (!table) return;

    // Check if already injected
    if (document.querySelector('#tcb-main-bar')) return;

    // Top Action Bar
    const topBar = document.createElement('div');
    topBar.id = 'tcb-main-bar';
    topBar.className = 'tcb-bar';
    topBar.innerHTML = `
      <div class="tcb-bar-title">
        <span>⚡ Tista Chargeback (Serverless Mode)</span>
        <span class="tcb-badge" id="tcb-total-kpi">Total: $0.00 (0 orders)</span>
      </div>
      <div style="display:flex;gap:10px">
        <button id="tcb-btn-calc-all" class="tcb-btn" type="button">🔄 Recalculate Visible</button>
        <button id="tcb-btn-close-all" class="tcb-btn tcb-btn-danger" type="button">🔒 Charge & Close All Visible ($2.00 Min)</button>
      </div>
    `;

    table.parentNode.insertBefore(topBar, table);

    // Add Table Header Columns
    const headerRow = table.querySelector('thead tr');
    if (headerRow && !headerRow.querySelector('.tcb-th-cb')) {
      const thCb = document.createElement('th');
      thCb.className = 'tcb-th-cb';
      thCb.textContent = 'Chargeback ($)';
      headerRow.insertBefore(thCb, headerRow.lastElementChild);

      const thAct = document.createElement('th');
      thAct.className = 'tcb-th-act';
      thAct.textContent = 'Quick Action';
      headerRow.appendChild(thAct);
    }

    processTableRows(table);

    document.getElementById('tcb-btn-calc-all').addEventListener('click', () => processTableRows(table));
    document.getElementById('tcb-btn-close-all').addEventListener('click', () => batchCloseVisible(table));
  }

  function processTableRows(table) {
    const rows = table.querySelectorAll('tbody tr');
    let totalCharge = 0;
    let count = 0;

    rows.forEach(tr => {
      const cells = tr.querySelectorAll('td');
      if (cells.length < 5) return;

      const disputeId = cells[0].textContent.trim();
      const orderId = cells[1].textContent.trim().replace('#', '');
      const location = cells[2]?.textContent.trim() || '';
      const reason = cells[4]?.textContent.trim().toLowerCase() || 'correction';
      const faultQty = parseFloat(cells[9]?.textContent.trim()) || 1;
      const severityStr = (cells[10]?.textContent.trim() || 'low').toLowerCase();
      const statusText = cells[11]?.textContent.trim().toLowerCase() || '';

      const isClosed = statusText.includes('close');
      const sevMult = SEV_MAP[severityStr] || 1.5;

      // Default service & category for calculation
      let svc = tr.dataset.tcbSvc || 'Clipping path';
      let cat = tr.dataset.tcbCat || 'Category 1';
      let pix = '> 5000';

      const unitPr = getPrice(svc, cat, pix);
      let calculated = unitPr * sevMult * faultQty;
      let finalAmt = Math.max(2.0, calculated);
      let minApplied = calculated < 2.0;

      totalCharge += finalAmt;
      count++;

      tr.dataset.tcbDisputeId = disputeId;
      tr.dataset.tcbAmt = finalAmt.toFixed(2);

      // Injected Chargeback cell
      let cbCell = tr.querySelector('.tcb-td-cb');
      if (!cbCell) {
        cbCell = document.createElement('td');
        cbCell.className = 'tcb-td-cb';
        tr.insertBefore(cbCell, tr.lastElementChild);
      }

      cbCell.innerHTML = `
        <div style="font-family:monospace;font-weight:700;color:#20b2aa;font-size:13px">
          $${finalAmt.toFixed(2)}
          ${minApplied ? '<span class="tcb-min-badge">Min $2</span>' : ''}
        </div>
      `;

      // Injected Action cell
      let actCell = tr.querySelector('.tcb-td-act');
      if (!actCell) {
        actCell = document.createElement('td');
        actCell.className = 'tcb-td-act';
        tr.appendChild(actCell);
      }

      if (isClosed) {
        actCell.innerHTML = `<span class="tcb-closed-badge">✓ Closed</span>`;
      } else {
        actCell.innerHTML = `
          <button type="button" class="tcb-btn tcb-btn-danger tcb-btn-sm tcb-row-close" data-did="${disputeId}" data-amt="${finalAmt.toFixed(2)}">
            🔒 Close
          </button>
        `;

        const btn = actCell.querySelector('.tcb-row-close');
        btn.addEventListener('click', async (e) => {
          e.stopPropagation();
          if (!confirm(`Charge $${finalAmt.toFixed(2)} to Vendor and Close Dispute #${disputeId}?`)) return;
          btn.disabled = true;
          btn.textContent = 'Updating...';
          try {
            await closeDisputeDirect(disputeId, finalAmt);
            actCell.innerHTML = `<span class="tcb-closed-badge">✓ Closed</span>`;
            if (cells[11]) cells[11].textContent = 'Closed';
          } catch (err) {
            alert('Error closing dispute: ' + err.message);
            btn.disabled = false;
            btn.textContent = '🔒 Close';
          }
        });
      }
    });

    const kpi = document.getElementById('tcb-total-kpi');
    if (kpi) kpi.textContent = `Total: $${totalCharge.toFixed(2)} (${count} orders)`;
  }

  async function batchCloseVisible(table) {
    const rows = table.querySelectorAll('tbody tr');
    const toClose = [];

    rows.forEach(tr => {
      const did = tr.dataset.tcbDisputeId;
      const amt = parseFloat(tr.dataset.tcbAmt) || 2.0;
      const act = tr.querySelector('.tcb-td-act');
      if (did && act && act.querySelector('.tcb-row-close')) {
        toClose.push({ tr, did, amt });
      }
    });

    if (!toClose.length) {
      alert('All visible disputes are already closed.');
      return;
    }

    const totalAmt = toClose.reduce((acc, it) => acc + it.amt, 0);
    if (!confirm(`Are you sure you want to Charge to Vendor and Close ${toClose.length} disputes?\nTotal: $${totalAmt.toFixed(2)}`)) {
      return;
    }

    const btn = document.getElementById('tcb-btn-close-all');
    if (btn) btn.disabled = true;

    let successCount = 0;
    for (let i = 0; i < toClose.length; i++) {
      const item = toClose[i];
      if (btn) btn.textContent = `Processing (${i + 1}/${toClose.length})...`;
      try {
        await closeDisputeDirect(item.did, item.amt);
        const actCell = item.tr.querySelector('.tcb-td-act');
        if (actCell) actCell.innerHTML = `<span class="tcb-closed-badge">✓ Closed</span>`;
        const cells = item.tr.querySelectorAll('td');
        if (cells[11]) cells[11].textContent = 'Closed';
        successCount++;
      } catch (e) {
        console.error('Failed to close dispute', item.did, e);
      }
    }

    alert(`✓ Completed! Successfully charged & closed ${successCount} of ${toClose.length} disputes on Tista.`);
    if (btn) {
      btn.disabled = false;
      btn.textContent = '🔒 Charge & Close All Visible ($2.00 Min)';
    }
  }

})();
