# Chargeback Calculator & Tista Automation

Correction and delay delivery chargebacks calculation, integrating directly with **tista.app**.

---

## ⚡ 1. Automatic Delay Delivery Calculation

When an Order ID is provided, the automation performs the following steps:

1. **Authentication:**
   - Navigates to `https://tista.app/`.
   - Uses a persistent browser profile (`~/.tista-profile`), so you only log in once.
2. **Dispute Search:**
   - Navigates to `https://tista.app/manage/vendors/1/order_disputes?q[location_id_eq]=1`.
   - Searches for `#<Order_ID>` in `#q_id_or_fulfillment_order_order_name_eq`.
   - Filters:
     - **Dispute reason:** `Delay`
     - **Location:** `All Location`
3. **Dispute Data Extraction:**
   - **Contractor Name:** Extracted from `Dispute location`.
   - **Delay time difference (Second):** Extracted from `Delayed by` (converted from `hh:mm:ss` to negative seconds, e.g. `01:43:05` $\rightarrow$ `-6185s`).
   - **Quantity of Service:** Extracted from `Fault quantity`.
4. **Order Files Navigation:**
   - Searches the same Order ID in the top navbar (`//*[@id="navbar"]/form/div[1]/input`) or locates order row `order-{id}`.
   - Opens order files and reads `Order Duration` and `Pixel Range`.
5. **Services & Category Extraction:**
   - Clicks **Filter** &rarr; **Services** dropdown.
   - Extracts all categories (e.g. `Category 1`, `Category 2`, `Category 2.5`).
   - Calculates the **Average Category** and sets the closest **Category Level**.
   - Detects the primary **Service Type**.
6. **Live Calculation & Rules:**
   - Unit price looked up from Price List.
   - Delay percentage calculated: $|\text{delay seconds}| \div 86400 \div \text{order duration (days)}$.
   - Delay % Tier applied (e.g. 5%–15% $\rightarrow$ 40% share).
   - **Minimum $2.00 Rule:** If calculated chargeback is between $0.01 and $1.99, it is automatically raised to **$2.00**.

---

## 📁 2. Automatic Correction Calculation (Quick Auto-Fetch & File Selection)

### A. Quick Auto-Fetch in Correction Tab (Immediate Calculation):
- In the **Correction** tab, enter an Order ID (e.g. `457531`) in **⚡ Quick Auto-Fetch:** and click **⚡ Fetch & Calculate**.
- Directly queries Tista, extracts contractor, service type, category level, fault quantity, fault severity, and pixel range.
- Immediately calculates the Unit Price and Total Chargeback right inside the **Correction** tab without redirecting to other tabs.
- Ready to click **🔒 Charge to Vendor & Close Dispute on Tista**.
- If individual file selection is desired, click **Select Files** to open the full interactive files table.

### B. Interactive File Selection (Auto Correction Tab):
For correction disputes requiring fine-grained file selection (e.g. Order `#457696`):

1. **Order Files Table View:**
   - Navigates to `https://tista.app/orders/{order_id}/files`.
   - Automatically switches to Table Layout (`button[data-order-files-layout-param="table"]`).
   - Extracts every file row with:
     - **Filename**
     - **Services** (with badges `C1`, `C1.5`, `C2`, etc.)
     - **Location** (Contractor)
     - **Dimension** (`>5000px`)
2. **Interactive File Selection:**
   - Displays all files in a clean, responsive table with checkboxes.
   - User can check/uncheck individual files or use **Select All** / **Deselect All**.
   - Includes real-time search filter for quick filename/service matching.
3. **Dynamic Category & Chargeback Calculation:**
   - **Quantity:** Equal to the number of checked files.
   - **Service Type:** Dropdown of services present in the selected files.
   - **Average Category:** Automatically computes the numeric average of categories across selected files for that service (e.g. all `C1.5` $\rightarrow$ Average `1.50` $\rightarrow$ `Category 1.5`).
   - **Category Level:** Auto-selected, with manual override available.
   - **Severity Level:** Dropdown (`Minor 150%`, `Medium 200%`, `Major 250%`, `Multiple 400%`).
   - **Unit Price:** Automatically looked up from the official Price List.
   - **Chargeback Formula:**
     $$\text{Chargeback} = \text{Unit Price} \times \text{Severity \%} \times \text{Quantity}$$
   - **Minimum $2.00 Rule:** Applies to any calculated amount below $2.00.
4. **Apply to Calculator:**
   - Clicking **Apply to Correction Calculator** transfers all values into the main Correction tab.

---

---

## 📋 3. Batch Dispute Search & Calculation System

Allows batch searching and calculating disputes directly from Tista (`https://tista.app/manage/vendors/1/order_disputes`):

1. **Search & Filter Controls:**
   - **Fault at (Date Range):** Filter disputes by date using Start Date and End Date pickers, or quick preset buttons (**Today**, **7 Days**, **14 Days**, **This Month**, **Clear**). Automatically formats to Tista's expected flatpickr format (`DD/MM/YY - DD/MM/YY`).
   - **Search Query:** Supports Dispute ID (e.g. `47359`), Order ID (e.g. `#458192` or `458192`), or multiple IDs (comma/space/newline separated).
   - **Dispute Reason:** `Correction`, `Delay`, `Quality`, `All Reasons`, `Revision`, `Technical fault`.
   - **Status:** `Awaiting response from vendor`, `Under review`, `All statuses`.
   - **Location:** `All locations`, `Path`, `Creative Path (P1)`, `Pixel Path (P2)`, `Perfect Pixel (GB2)`, `Vectorio`, `Cutout Edge`, `Edgebo`, `ClippingPix`, etc.
   - **Chargeable to:** `Vendor`, `All`.
   - **Deep Scan:** Toggle to deep scan files for every order in the list.
2. **Unified Chargeback Calculation Rules:**
   - **Quality & Correction:**
     $$\text{Chargeback} = \text{Unit Price} \times \text{Severity \%} \times \text{Fault Quantity}$$
     Severity mapping: `Low` $\rightarrow$ `Minor 150%`, `Medium` $\rightarrow$ `Medium 200%`, `High` / `Critical` $\rightarrow$ `Major 250%`.
   - **Delay:**
     $$\text{Chargeback} = \text{Unit Price} \times \text{Delay Tier \%} \times \text{Fault Quantity}$$
     `Delayed by` hours converted to seconds, compared against order duration to determine Delay % tier (20%, 40%, 60%, 80%, 100%).
   - **Minimum $2.00 Rule:** Every dispute chargeback is guaranteed to be at least **$2.00**.
3. **Disputes Summary & Table View:**
   - Displays metrics: Total Disputes, Total Chargeback ($), Min $2 Applied count, Total Fault Quantity.
   - Contractor breakdown pills showing count and dollar total per contractor.
   - Table columns: `#`, `Order ID`, `Fault at`, `Reason`, `Contractor`, `Service` (interactive dropdown), `Category` (interactive dropdown), `Severity / Tier`, `Qty`, `Unit Price`, `Chargeback ($)`, `Status`, and an **Open** button.
   - **Interactive Dropdowns:** Service and Category can be edited directly in the table row; any change immediately recalculates the Unit Price, Chargeback amount, Min $2 guarantee, and batch KPIs.
   - Clicking **Open** immediately opens that specific dispute in the Correction or Delay calculator prefilled with the chosen service and category.
   - **Export CSV** and **Copy Summary Table** buttons for clean reporting.

---

## 🔍 4. Dispute ID vs Order ID Auto-Resolution & Fix for Warning Messages

In Tista:
- **Order IDs** have a `#` prefix (e.g. `#458305`, `#458192`) in the dispute table.
- **Dispute IDs** are 5-digit numbers without `#` (e.g. `47374`, `47359`).
- Previously, searching a Correction dispute inside the Delay tab or searching with missing `#` triggered 5 red error banners:
  1. *Dispute location was not found in contractor list. Please verify.*
  2. *Delayed by was not found.*
  3. *Fault quantity was not found.*
  4. *Order duration was not found. Enter it manually in Delay tab.*
  5. *Pixel range was not found. Select it manually.*
- **Fix Implemented:**
  - Automated candidate queries try both `#<ID>` and `<ID>` against `#q_id_or_fulfillment_order_order_name_eq`.
  - Searches without locking `dispute_reason` to Delay, allowing both Correction and Quality disputes to be retrieved.
  - Added `Path` and `Subs` location mappings.
  - When a Correction dispute is fetched in the Auto tab, it detects the reason, shows the Correction calculation summary, and provides a 1-click button to apply to the Correction calculator.

---

## 🔒 5. Charge to Vendor & Close Dispute Automation

Directly closes disputes on Tista while charging the calculated amount to the vendor:

1. **How It Works on Tista:**
   - Navigates to `https://tista.app/manage/vendors/1/order_disputes/{dispute_id}/edit`
   - Sets **Chargeable to:** `Vendor` (`#order_dispute_chargeable_to` $\rightarrow$ `vendor`)
   - Sets **Charge to vendor:** Calculated chargeback amount with minimum $2.00 rule (`#order_dispute_charge_to_vendor` $\rightarrow$ `$X.XX`)
   - Sets **Status:** `Closed` (`#order_dispute_status` $\rightarrow$ `closed`)
   - Submits `Update Order dispute` and confirms any prompt
2. **Single Dispute Closing:**
   - **Correction Tab:** Click **🔒 Charge to Vendor & Close Dispute on Tista** under Total Chargeback Amount.
   - **Delay Tab:** Click **🔒 Charge to Vendor & Close Dispute on Tista** under Total Chargeback Amount.
   - Automatically resolves `dispute_id` if only `order_id` is known.
3. **Batch Dispute Closing:**
   - **Per Row:** In the Batch disputes table, click the red **🔒 Close** button in the Action column. Once closed, displays green **✓ Closed**.
   - **All Visible:** Click **🔒 Charge & Close All Visible** in the top toolbar to close all unclosed disputes sequentially with live progress and automatic table status update.

---

## 🚀 How to Run

### Option 1: One-Click Web App (Recommended)
1. Double-click **`start_calculator.bat`**.
2. It starts the local API server and automatically opens `http://127.0.0.1:8765/Chargeback%20Calculator.html` in your browser.
3. For Delay disputes: Go to **Auto Delay** tab or use **⚡ Quick Auto-Fetch** on the Delay tab.
4. For Correction disputes: Go to **Auto Correction** tab or use **⚡ Quick Auto-Fetch** on the Correction tab.
5. Enter the Order ID (e.g. `458305` or `457696`) and click **Fetch**.

### Option 2: Command Line / Batch Scripts
- **One-time Tista Login:**
  ```bash
  login_tista.bat
  ```
- **Fetch Delay Data CLI:**
  ```bash
  py -3.11 tista_delay_fetch.py 458305
  ```
- **Fetch Correction Files CLI:**
  ```bash
  py -3.11 tista_delay_fetch.py --files 457696
  ```
- **Offline / Sample Testing:**
  You can click **Load Order #457696 (Sample)** inside the Auto Correction tab anytime to test without an active internet connection.
