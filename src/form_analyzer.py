"""DOM inspector for Indeed Easy Apply form fields.

Scans the current form page and returns structured representations of all
visible input fields, including their labels, types, options, and current values.
"""

from dataclasses import dataclass, field
from playwright.async_api import Page


@dataclass
class FormField:
    """Structured representation of a single form field."""
    element_id: str = ""
    field_type: str = ""       # text, textarea, select, radio, checkbox, file, tel, email, number
    label_text: str = ""
    placeholder: str = ""
    required: bool = False
    options: list[str] = field(default_factory=list)
    current_value: str = ""
    selector: str = ""
    name: str = ""


async def analyze_form_page(page: Page) -> list[FormField]:
    """Inspect the current form page and return all visible input fields with metadata.

    Uses JavaScript evaluation to scan the DOM for all form inputs, extracting
    their labels, types, current values, and available options.

    Returns:
        List of FormField objects representing each fillable field on the page.
    """
    raw_fields = await page.evaluate("""() => {
        const fields = [];
        const seen = new Set();

        function getLabel(el) {
            // Try multiple strategies to find the label text for an input
            // 1. Explicit <label for="id">
            if (el.id) {
                const label = document.querySelector('label[for="' + el.id + '"]');
                if (label) return label.textContent.trim();
            }
            // 2. Closest parent label
            const parentLabel = el.closest('label');
            if (parentLabel) return parentLabel.textContent.trim();
            // 3. aria-label
            if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
            // 4. aria-labelledby
            const labelledBy = el.getAttribute('aria-labelledby');
            if (labelledBy) {
                const refEl = document.getElementById(labelledBy);
                if (refEl) return refEl.textContent.trim();
            }
            // 5. Previous sibling text or parent fieldset legend
            const fieldset = el.closest('fieldset');
            if (fieldset) {
                const legend = fieldset.querySelector('legend');
                if (legend) return legend.textContent.trim();
            }
            // 6. Preceding text node or element
            const prev = el.previousElementSibling;
            if (prev && prev.textContent.trim().length < 200) {
                return prev.textContent.trim();
            }
            return '';
        }

        // Scan standard form elements
        const formEls = document.querySelectorAll(
            'input:not([type="hidden"]):not([type="submit"]):not([type="button"]), ' +
            'select, textarea'
        );

        formEls.forEach(el => {
            if (!el.offsetParent) return; // skip hidden elements
            // Radios are handled by the grouped scan below — pushing them
            // individually here labels each option as its own question, which
            // confuses the filler and the audit log. Skip them here.
            if (el.type === 'radio') return;
            const key = el.id || el.name || el.getAttribute('data-testid') || '';
            if (key && seen.has(key)) return;
            if (key) seen.add(key);

            const type = el.tagName.toLowerCase() === 'select' ? 'select' :
                         el.tagName.toLowerCase() === 'textarea' ? 'textarea' :
                         (el.type || 'text').toLowerCase();

            let options = [];
            if (type === 'select') {
                options = [...el.options].map(o => o.text.trim()).filter(t => t && t !== '');
            }

            // Build a reliable selector
            let selector = '';
            if (el.id) selector = '#' + CSS.escape(el.id);
            else if (el.name) selector = '[name="' + el.name + '"]';
            else if (el.getAttribute('data-testid')) selector = '[data-testid="' + el.getAttribute('data-testid') + '"]';

            fields.push({
                id: el.id || '',
                name: el.name || '',
                type: type,
                label: getLabel(el),
                required: el.required || el.getAttribute('aria-required') === 'true',
                value: el.value || '',
                options: options,
                placeholder: el.placeholder || '',
                selector: selector
            });
        });

        // Also scan for radio button groups (grouped by name)
        const radioGroups = {};
        document.querySelectorAll('input[type="radio"]').forEach(radio => {
            if (!radio.offsetParent) return;
            const name = radio.name;
            if (!name) return;
            if (!radioGroups[name]) {
                radioGroups[name] = {
                    label: '',
                    options: [],
                    required: radio.required,
                    checked: ''
                };
                // Find group label from fieldset/legend or aria
                const fieldset = radio.closest('fieldset');
                if (fieldset) {
                    const legend = fieldset.querySelector('legend');
                    if (legend) radioGroups[name].label = legend.textContent.trim();
                }
                // Walk up to find a question text (Indeed nests the prompt
                // in a div/p above the radio group, not in a <legend>).
                if (!radioGroups[name].label) {
                    let node = radio.parentElement;
                    for (let depth = 0; depth < 6 && node; depth++, node = node.parentElement) {
                        // Look for a previous sibling that holds the prompt text.
                        let sib = node.previousElementSibling;
                        while (sib) {
                            const t = (sib.innerText || sib.textContent || '').trim();
                            if (t && t.length > 3 && t.length < 400 && !/^(yes|no)$/i.test(t)) {
                                radioGroups[name].label = t;
                                break;
                            }
                            sib = sib.previousElementSibling;
                        }
                        if (radioGroups[name].label) break;
                        // Also check the parent itself for a legend-like child.
                        const candidate = node.querySelector('legend, [role="heading"], label, .question');
                        if (candidate && candidate.contains(radio) === false) {
                            const t = (candidate.innerText || candidate.textContent || '').trim();
                            if (t && t.length > 3 && t.length < 400) {
                                radioGroups[name].label = t;
                                break;
                            }
                        }
                    }
                }
                if (!radioGroups[name].label) {
                    radioGroups[name].label = getLabel(radio);
                }
            }
            const optionLabel = getLabel(radio) || radio.value;
            radioGroups[name].options.push(optionLabel);
            if (radio.checked) radioGroups[name].checked = optionLabel;
        });

        for (const [name, group] of Object.entries(radioGroups)) {
            if (seen.has('radio_' + name)) continue;
            seen.add('radio_' + name);
            fields.push({
                id: '',
                name: name,
                type: 'radio',
                label: group.label,
                required: group.required,
                value: group.checked,
                options: group.options,
                placeholder: '',
                selector: 'input[name="' + name + '"]'
            });
        }

        return fields;
    }""")

    form_fields = []
    for f in raw_fields:
        form_fields.append(FormField(
            element_id=f.get("id", ""),
            field_type=f.get("type", "text"),
            label_text=f.get("label", ""),
            placeholder=f.get("placeholder", ""),
            required=f.get("required", False),
            options=f.get("options", []),
            current_value=f.get("value", ""),
            selector=f.get("selector", ""),
            name=f.get("name", ""),
        ))

    print(f"  [form] Analyzed page: found {len(form_fields)} fields")
    for ff in form_fields:
        label_preview = ff.label_text[:60] if ff.label_text else "(no label)"
        val_preview = f" = '{ff.current_value[:30]}'" if ff.current_value else ""
        req = " *" if ff.required else ""
        print(f"    [{ff.field_type:8}]{req} {label_preview}{val_preview}")

    return form_fields


async def is_review_page(page: Page) -> bool:
    """Check if the current page is the final review/submit page."""
    # URL-based check first — Indeed's actual review step uses /review-module/
    # in the path. Breadcrumbs ("Review your application") leak the phrase onto
    # earlier pages, so text matching alone is too noisy.
    if "/review-module" in page.url.lower() or "/review/" in page.url.lower():
        return True
    indicators = await page.evaluate("""() => {
        // Only a button explicitly labelled "Submit your application" counts —
        // generic Continue/Save buttons must not be misread as Submit.
        const submitBtns = Array.from(document.querySelectorAll('button'));
        return submitBtns.some(b => {
            const t = (b.textContent || '').trim().toLowerCase();
            return b.id === 'form-action-submit' ||
                t === 'submit application' ||
                t === 'submit your application';
        });
    }""")
    return bool(indicators)


async def is_confirmation_page(page: Page) -> bool:
    """Check if we've reached the post-submission confirmation page."""
    result = await page.evaluate("""() => {
        const text = document.body.innerText.toLowerCase();
        return text.includes('application submitted') ||
               text.includes('your application has been submitted') ||
               text.includes('successfully applied') ||
               text.includes('application sent');
    }""")
    return result


async def detect_already_applied(page: Page) -> bool:
    """Check if Indeed shows a 'You have already applied' message."""
    result = await page.evaluate("""() => {
        const text = document.body.innerText.toLowerCase();
        return text.includes('you have already applied') ||
               text.includes('already applied to this job');
    }""")
    return result
