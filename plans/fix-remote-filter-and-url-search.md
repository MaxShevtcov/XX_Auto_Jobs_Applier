# Plan: Fix Remote Filter + Switch to URL-Based Search

## Problem

1. **Remote filter disabled**: `search_config.yaml` has `REMOTE: false` (line 57)
2. **ALL hh.ru advanced search UI selectors are broken**: The `data-qa` selectors used in `_set_job_format`, `_set_experience`, `_set_area`, etc. no longer match hh.ru's current UI. Every filter silently fails — the search runs with no filters applied.
3. **Root cause**: hh.ru updated their frontend; the old `advanced-search__*` data-qa attributes were removed/renamed.

## Solution: Two-Phase Approach

### Phase 1: Quick Fix — Enable remote filter in config

**File**: `data_folder/search_config/search_config.yaml` line 57
- Change `REMOTE: false` → `REMOTE: true`

### Phase 2: Switch `set_advanced_search_params` to URL-based approach

Instead of clicking broken UI elements, construct the hh.ru search URL with query parameters directly.

#### hh.ru URL Parameter Mapping

| Config Field | hh.ru URL Param | Example |
|---|---|---|
| `keywords` | `text` | `?text=android` |
| `words_to_exclude` | `text` (prefix with `-`) | `?text=-legacy+-deprecated` |
| `search_field.name` or `search_field.company_name` | `search_field=name` or `search_field=company_name` | `?search_field=name` |
| `search_field.description` | `search_field=description` | `?search_field=description` |
| `experience` (one true key) | `experience` | `?experience=doesNotMatter` |
| `employment` (true keys) | `employment_form` | `?employment_form=FULL` |
| `employment.INTERNSHIP` | `label=internship` | `?label=internship` |
| `employment.ACCEPT_TEMPORARY` | `label=accept_handicapped` — **need to verify** | |
| `job_format.REMOTE` | `remote=true` | `?remote=true` |
| `job_format.HYBRID` | `work_format=HYBRID` | `?work_format=HYBRID` |
| `job_format.ON_SITE` | `work_format=ON_SITE` | `?work_format=ON_SITE` |
| `job_format.FIELD_WORK` | `work_format=FIELD_WORK` | `?work_format=FIELD_WORK` |
| `education` (true keys) | `education` | `?education=higher` |
| `salary` | `salary` | `?salary=150000` |
| `currency` (one true key) | `currency_code` | `?currency_code=RUR` |
| `only_with_salary` | `label=with_salary` | `?label=with_salary` |
| `vacancy_label` (true keys) | `label` | `?label=with_address&label=accept_handicapped` |
| `order_by` (one true key) | `order_by` | `?order_by=publication_time` |
| `period` (one true key) | `search_period` | `?search_period=30` |
| `show` (one true key) | `items_on_page` | `?items_on_page=50` |
| `area` | `area` | `?area=113` (needs area name → ID resolution) |
| `professional_role` | `professional_role` | `?professional_role=96` (needs name → ID resolution) |
| `industry` | `industry` | `?industry=13` (needs name → ID resolution) |
| `districts` | `district` | `?district=131` (needs name → ID resolution) |

**Note on `area`, `professional_role`, `industry`, `districts`**: These require resolving human-readable names to numeric hh.ru IDs. The UI approach for these fields was done via tree-selector modals. For the URL approach, we have two options:
- **Option A**: Keep the tree-selector UI approach for these 4 fields only (they use different selectors than the broken `advanced-search__*` ones)
- **Option B**: Use the hh.ru API to resolve names to IDs (but the API is not currently used in the codebase)

**Recommended**: Option A — keep tree-selector UI for `area`, `professional_role`, `industry`, `districts`; use URL for everything else.

### Implementation Steps

#### Step 1: Fix config
- `data_folder/search_config/search_config.yaml:57` — `REMOTE: true`

#### Step 2: Add `_build_search_url` method to `PlaywrightJobManager`
- **File**: `src/job_manager/playwright_manager.py`
- **New method**: `_build_search_url(self, search_params: dict) -> str`
- Reads all search_params, builds query dict, returns `https://hh.ru/search/vacancy?{params}`
- Handles the param → URL mapping from the table above
- Uses `urllib.parse.urlencode` with `doseq=True` for multi-value params

#### Step 3: Rewrite `set_advanced_search_params`
- **File**: `src/job_manager/playwright_manager.py` (lines 338-411)
- Replace UI-based approach with:
  1. Call `start_search(resume_id)` (navigate to resume page, click "Подобрали для вас")
  2. Call `_build_search_url(search_params)` to construct URL
  3. Navigate to the URL via `safe_goto`
  4. For `area`, `professional_role`, `industry`, `districts` — these still need name→ID resolution. Two sub-approaches:
     - **Keep tree-selector** for these fields (open advanced search, set these via UI, then trigger search)
     - **Or** skip them for now (if user doesn't use them) and document as TODO

#### Step 4: Write tests
- **File**: `tests/test_playwright_manager.py` (or new file)
- Test `_build_search_url` with various config combinations
- Test that URL contains correct params for remote, experience, salary, etc.

### Files to Modify

| File | Change |
|---|---|
| `data_folder/search_config/search_config.yaml:57` | `REMOTE: true` |
| `src/job_manager/playwright_manager.py` | Add `_build_search_url()`, rewrite `set_advanced_search_params()` |

### Risk Assessment

- **Low risk**: Config change is trivial
- **Medium risk**: URL-based approach changes the core search flow. Must verify:
  - hh.ru URL params are correct (verified via web fetch: `remote=true` works)
  - Pagination still works (it already uses URL-based navigation)
  - `professional_role`, `area`, `industry` resolution still works (keep tree-selector approach for these)
- **Testing**: Run `pytest` after changes; manual verification on hh.ru

### Verification

1. `pre-commit run --all-files` (linting)
2. `pytest` (tests pass)
3. Manual check: set `REMOTE: true`, run search, verify only remote vacancies appear
