import asyncio
import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
from playwright.async_api import Page, Frame
from typing import Dict, List, Optional, Any, Tuple
import json
import os
import hashlib
import re
import config
from run_context import compute_question_fingerprint, compute_logical_question_id

async def scrape_frame(frame: Frame, frame_index: int = 0):
    """
    Tier 1 Semantic Scraper.
    Extracts semantic information about interactive elements without mutating the DOM.
    Returns data needed for Playwright targeting, with stable question grouping and fingerprints.
    """
    js_script = r"""
    () => {
        const results = [];
        const radioGroups = {};
        const checkGroups = {};
        
        function getStableContainerKey(element, prefix = 'cont') {
            if (!element) return '';
            if (element.id) return `${prefix}_id_${element.id}`;
            if (element.getAttribute('name')) return `${prefix}_name_${element.getAttribute('name')}`;
            if (element.getAttribute('data-testid')) return `${prefix}_dt_${element.getAttribute('data-testid')}`;
            if (element.getAttribute('aria-labelledby')) return `${prefix}_lbl_${element.getAttribute('aria-labelledby')}`;
            if (element.tagName && element.tagName.toLowerCase() === 'fieldset') {
                const leg = element.querySelector('legend');
                if (leg && leg.innerText.trim()) {
                    return `${prefix}_leg_${leg.innerText.trim().replace(/\s+/g, '_').substring(0, 30)}`;
                }
            }
            const parentFieldset = element.closest('fieldset, [role="group"], [role="radiogroup"]');
            if (parentFieldset && parentFieldset !== element) {
                if (parentFieldset.id) return `${prefix}_pfs_${parentFieldset.id}`;
                const leg = parentFieldset.querySelector('legend');
                if (leg && leg.innerText.trim()) {
                    return `${prefix}_pfsleg_${leg.innerText.trim().replace(/\s+/g, '_').substring(0, 30)}`;
                }
            }
            let pos = 0;
            let sib = element.previousElementSibling;
            while (sib) {
                if (sib.tagName === element.tagName) pos++;
                sib = sib.previousElementSibling;
            }
            const pTag = element.parentElement ? element.parentElement.tagName.toLowerCase() : 'root';
            const pId = element.parentElement && element.parentElement.id ? element.parentElement.id : '';
            return `${prefix}_${pTag}${pId ? '_' + pId : ''}_${element.tagName.toLowerCase()}_${pos}`;
        }
        
        // Comprehensive selector for standard inputs, ARIA widgets, custom choice buttons, rating scales, sliders, and signatures
        const selectors = [
            'input',
            'select',
            'textarea',
            '[contenteditable="true"]',
            '[role="textbox"]',
            '[role="radio"]',
            '[role="checkbox"]',
            '[role="listbox"]',
            '[role="combobox"]',
            '[role="slider"]',
            '[role="datepicker"]',
            'button[role="radio"]',
            'button[role="checkbox"]',
            'button[aria-haspopup="listbox"]',
            'button[data-choice]',
            'button.choice-btn',
            'button.btn-option',
            '.scale-item',
            '.rating-btn',
            '[data-rating]',
            '.ui-slider-handle',
            'canvas.SignatureCanvas',
            'canvas[id*="Signature" i]',
            'canvas[id*="sig" i]',
            '.SignatureBox canvas',
            'canvas[class*="signature" i]',
            'canvas[class*="sig" i]',
            'canvas'
        ].join(', ');
        
        function collectElements(root) {
            let found = Array.from(root.querySelectorAll(selectors));
            const allNodes = root.querySelectorAll('*');
            allNodes.forEach(node => {
                if (node.shadowRoot) {
                    found = found.concat(collectElements(node.shadowRoot));
                }
            });
            return found;
        }

        const elements = collectElements(document);
        
        elements.forEach((el, index) => {
            // Visibility check: allow native inputs hidden by CSS if they have a visible label or container
            const isHidden = (el.offsetWidth === 0 && el.offsetHeight === 0) || 
                             window.getComputedStyle(el).display === 'none' || 
                             window.getComputedStyle(el).visibility === 'hidden';
            
            let hasVisibleLabel = false;
            if (el.closest('label')) {
                const pLbl = el.closest('label');
                if ((pLbl.offsetWidth > 0 || pLbl.offsetHeight > 0) && window.getComputedStyle(pLbl).display !== 'none') {
                    hasVisibleLabel = true;
                }
            }
            if (!hasVisibleLabel && el.id) {
                try {
                    const labels = document.querySelectorAll(`label[for="${CSS.escape(el.id)}"]`);
                    for (const l of labels) {
                        if ((l.offsetWidth > 0 || l.offsetHeight > 0) && window.getComputedStyle(l).display !== 'none') {
                            hasVisibleLabel = true;
                            break;
                        }
                    }
                } catch(e) {}
            }
            
            const isChoiceElement = el.type === 'radio' || el.type === 'checkbox' || 
                                    el.getAttribute('role') === 'radio' || el.getAttribute('role') === 'checkbox' ||
                                    el.classList.contains('scale-item') || el.classList.contains('rating-btn') ||
                                    el.hasAttribute('data-choice');
                                    
            if (isChoiceElement && !hasVisibleLabel) {
                const parentContainer = el.closest('td, th, [role="gridcell"], .ControlContainer, .q-radio, .choice, .Selection, .QuestionBody, .ChoiceStructure, tr, li, div[class*="radio" i], div[class*="checkbox" i], .scale-item, .rating-btn');
                if (parentContainer && (parentContainer.offsetWidth > 0 || parentContainer.offsetHeight > 0) && 
                    window.getComputedStyle(parentContainer).display !== 'none' && 
                    window.getComputedStyle(parentContainer).visibility !== 'hidden') {
                    hasVisibleLabel = true;
                }
            }
            if (isHidden && !hasVisibleLabel) return;
            if (el.id && el.id.toLowerCase().startsWith('mobile_')) return;
            
            // Skip actual submit/action buttons, but keep choice buttons (note: <button> defaults to type='submit')
            if (el.type === 'hidden' || el.type === 'reset') return;
            if ((el.type === 'submit' || el.type === 'button') && !isChoiceElement) {
                // If it's a generic button and not inside a question choice container, check if it's navigation
                const bTxt = (el.innerText || el.value || '').trim().toLowerCase();
                const navWords = ['next', 'submit', 'continue', 'back', 'previous', 'start', 'begin', 'finish', 'done', 'save'];
                if (navWords.some(w => bTxt === w || bTxt.startsWith(w))) return;
                // If it doesn't look like a choice item, skip
                if (!el.closest('.cf-question, .question, .question-container, fieldset, [role="group"], [role="radiogroup"]')) {
                    return;
                }
            }
            if (el.classList.contains('hiddenradio')) return;
            
            // Skip security tokens, CSRF, and anti-bot verification inputs
            const elId = (el.id || '').toLowerCase();
            const elName = (el.name || '').toLowerCase();
            const secTokens = ['sessionid', 'verisoul', 'recaptcha', 'hcaptcha', 'turnstile', 'csrf', '_token', '__viewstate', 'fingerprint', 'bot_check', 'honeypot'];
            if (secTokens.some(k => elId.includes(k) || elName.includes(k))) return;
            
            // 1. Accessible Name / Option Label Extraction
            let name = el.getAttribute('aria-label') || '';
            
            if (isChoiceElement && el.id) {
                try {
                    const labels = document.querySelectorAll(`label[for="${CSS.escape(el.id)}"]`);
                    for (const l of labels) {
                        const txt = (l.innerText || '').trim();
                        if (txt) { name = txt; break; }
                    }
                } catch(e) {}
            }
            if (!name && isChoiceElement && el.closest('label')) {
                name = el.closest('label').innerText.trim();
            }
            if (!name && el.getAttribute('aria-labelledby')) {
                const idList = el.getAttribute('aria-labelledby').split(/\s+/);
                const targetIds = isChoiceElement && idList.length > 1 ? [idList[idList.length - 1]] : idList;
                const parts = targetIds.map(i => {
                    const found = document.getElementById(i);
                    return found ? found.innerText.trim() : '';
                }).filter(Boolean);
                if (parts.length > 0) name = parts.join(' ');
            }
            if (!name && el.id) {
                try {
                    const labels = document.querySelectorAll(`label[for="${CSS.escape(el.id)}"]`);
                    for (const l of labels) {
                        const txt = (l.innerText || '').trim();
                        if (txt) { name = txt; break; }
                    }
                } catch(e) {
                    try {
                        const labelEl = document.querySelector(`label[for="${el.id}"]`);
                        if (labelEl) name = labelEl.innerText;
                    } catch(e2) {}
                }
            }
            if (!name && el.closest('label')) {
                name = el.closest('label').innerText;
            }
            if (!name) {
                const prev = el.previousElementSibling;
                if (prev && (prev.tagName.toLowerCase() === 'label' || prev.innerText)) {
                    const pTxt = prev.innerText.trim();
                    if (pTxt && pTxt.length < 60) name = pTxt;
                } else if (el.parentElement) {
                    const parentText = Array.from(el.parentElement.childNodes)
                        .filter(n => n.nodeType === 3)
                        .map(n => n.textContent.trim())
                        .join(' ');
                    if (parentText && parentText.length < 60) name = parentText;
                }
            }
            if (!name && el.hasAttribute('placeholder')) {
                name = el.getAttribute('placeholder');
            }
            if (!name && el.hasAttribute('title')) {
                name = el.getAttribute('title');
            }
            
            const tag = el.tagName.toLowerCase();
            if (!name && tag !== 'select' && el.innerText && el.innerText.trim().length > 0) {
                name = el.innerText.substring(0, 100);
            }
            if (!name) {
                name = el.name || el.id || '';
            }
            name = (name || '').trim();
            const rawOptionName = name;
            
            // 2. Question Container and Title Extraction
            let contextHeading = "";
            const rgAncestor = el.closest('[role="radiogroup"], [role="group"], fieldset');
            if (rgAncestor && rgAncestor.getAttribute('aria-labelledby')) {
                const idList = rgAncestor.getAttribute('aria-labelledby').trim().split(/\s+/);
                for (const lid of idList) {
                    if (lid && !lid.includes('choice-display') && !lid.includes('choice-label')) {
                        const lblEl = document.getElementById(lid);
                        if (lblEl && lblEl.innerText.trim()) {
                            contextHeading = lblEl.innerText.trim();
                            break;
                        }
                    }
                }
            }
            if (!contextHeading && rgAncestor && rgAncestor.getAttribute('aria-label')) {
                contextHeading = rgAncestor.getAttribute('aria-label').trim();
            }

            let container = el.closest('.cf-question, .question, .question-container, tr[id*="-tr"], tr[sq_id], fieldset, [role="radiogroup"], [role="group"], .geS5n, .QuestionOuter, .question-content, .survey-question, div[data-testid*="question"], .freebirdFormviewerViewItemsItemItem, .form-line, .form-group, div[id*="box" i], div[id*="step" i], div[class*="question" i]:not([class*="answer" i]):not([class*="choice" i])');
            let searchEl = container || el;
            while (searchEl && searchEl !== document.body && !contextHeading) {
                const containerCandidate = searchEl.closest('.cf-question, .question, .question-container, tr[id*="-tr"], tr[sq_id], fieldset, [role="radiogroup"], [role="group"], .geS5n, .QuestionOuter, .question-content, .survey-question, div[data-testid*="question"], .freebirdFormviewerViewItemsItemItem, .form-line, .form-group, div[id*="box" i], div[id*="step" i], div[class*="question" i]:not([class*="answer" i]):not([class*="choice" i])');
                if (!containerCandidate) break;
                
                if (containerCandidate.tagName.toLowerCase() === 'fieldset') {
                    const leg = containerCandidate.querySelector('legend');
                    if (leg && leg.innerText.trim()) {
                        contextHeading = leg.innerText.trim();
                        break;
                    }
                }
                
                let headingEl = containerCandidate.querySelector('[data-kind="field-label"], .label-fl, td.labelrc:not(.questionnum), .question-display, [id*="question-display" i], [class*="question-display" i], .QuestionText, [class*="QuestionText" i], .title, .question-title, .question-title-container, .exportItemTitle, [data-testid*="title"], [data-testid*="header"], .cf-question__text, .cf-question__title, [class*="question-text" i], [class*="question__text" i], legend, label:not([for]), [role="heading"]:not(h1)');
                if (headingEl && headingEl.innerText.trim()) {
                    contextHeading = headingEl.innerText.trim();
                    break;
                }
                
                if (!containerCandidate.getAttribute('role')?.includes('group') && containerCandidate.tagName.toLowerCase() !== 'div') {
                    let firstLine = containerCandidate.innerText.split('\n')[0].trim();
                    if (firstLine.length > 3 && firstLine.length < 150 && !firstLine.toLowerCase().includes('change survey language')) {
                        contextHeading = firstLine;
                        break;
                    }
                }
                searchEl = containerCandidate.parentElement;
            }
            if (!contextHeading) {
                const singlePageHeading = document.querySelector('.cf-question__text, .cf-question__title, [class*="question__text" i], [class*="question-text" i], .QuestionText');
                if (singlePageHeading) contextHeading = singlePageHeading.innerText.trim();
            }
            
            // Required check
            let isRequired = el.required || el.getAttribute('aria-required') === 'true';
            if (!isRequired && container) {
                const reqEl = container.querySelector('.required, .req, [aria-label*="required" i], span.asterisk');
                if (reqEl || container.innerText.includes('*')) {
                    isRequired = true;
                }
            }
            
            // 3. Determine Role
            let role = el.getAttribute('role');
            if (!role) {
                if (tag === 'canvas' || el.classList.contains('SignatureCanvas') || (el.id && el.id.toLowerCase().includes('sig'))) {
                    role = 'signature';
                    if (!name) name = contextHeading || 'Sign Here';
                } else if (tag === 'input') {
                    if (el.type === 'checkbox') role = 'checkbox';
                    else if (el.type === 'radio') role = 'radio';
                    else if (el.type === 'range') role = 'slider';
                    else if (el.type === 'date' || el.type === 'time' || el.type === 'datetime-local') role = 'datepicker';
                    else if (el.type === 'file') role = 'file_upload';
                    else role = 'textbox';
                } else if (tag === 'select') {
                    role = 'combobox';
                } else if (tag === 'textarea' || el.isContentEditable) {
                    role = 'textbox';
                } else if (el.classList.contains('scale-item') || el.classList.contains('rating-btn') || el.hasAttribute('data-rating')) {
                    role = 'radio';
                } else if (el.hasAttribute('data-choice') || el.classList.contains('choice-btn')) {
                    role = 'radio';
                } else if (el.getAttribute('aria-haspopup') === 'listbox') {
                    role = 'combobox';
                } else if (el.classList.contains('ui-slider-handle')) {
                    role = 'slider';
                }
            }
            
            // Inject stable ID for locator targeting
            const injectedId = `stillbot_f${__FRAME_IDX__}_${index}`;
            el.setAttribute('data-stillbot-id', injectedId);
            
            // 4. Matrix / Grid Question Detection
            const tr = el.closest('tr, [role="row"]');
            const table = el.closest('table, [role="grid"], .cf-grid, .cf-grid-table');
            let rowLabel = "";
            let colLabel = "";
            let isMatrix = false;
            
            const rg = el.closest('[role="radiogroup"]');
            const ariaLbl = el.getAttribute('aria-label') || '';
            if (ariaLbl.includes(', response for ') || ariaLbl.includes(', response to ')) {
                isMatrix = true;
                const sep = ariaLbl.includes(', response for ') ? ', response for ' : ', response to ';
                const parts = ariaLbl.split(sep);
                colLabel = parts[0].trim();
                if (parts.length > 1 && parts[1].trim()) rowLabel = parts[1].trim();
            }
            if (!rowLabel && rg && rg.getAttribute('aria-label') && (tr || table)) {
                rowLabel = rg.getAttribute('aria-label').trim();
                if (rowLabel) isMatrix = true;
            }
            if (tr && table && !isMatrix) {
                isMatrix = true;
                const rowLabelEl = tr.querySelector('.cf-grid-row__label, .cf-label, th, td:first-child');
                if (rowLabelEl && rowLabelEl !== el.closest('td')) {
                    rowLabel = rowLabelEl.innerText.trim();
                }
                const td = el.closest('td, [role="gridcell"]');
                if (td) {
                    const colIdx = Array.from(tr.children).indexOf(td);
                    const theadThs = table.querySelectorAll('thead th, thead td, tr:first-child th, tr:first-child td, .cf-grid-col__label');
                    if (theadThs[colIdx]) {
                        colLabel = theadThs[colIdx].innerText.trim();
                    }
                }
            }
            
            // 5. Radio Buttons Aggregation
            if (role === 'radio') {
                let groupKey = "";
                if (rg) {
                    groupKey = getStableContainerKey(rg, 'rg');
                } else if (el.name) {
                    groupKey = 'name_' + el.name;
                } else if (container) {
                    groupKey = getStableContainerKey(container, 'cont');
                } else if (tr) {
                    groupKey = getStableContainerKey(tr, 'row');
                } else {
                    groupKey = 'single_' + index;
                }

                const optText = colLabel || rawOptionName || el.value || `Option ${index}`;
                let qLabel = contextHeading;
                if (!qLabel && rg && rg.getAttribute('aria-labelledby')) {
                    const rgLblIds = rg.getAttribute('aria-labelledby').trim().split(/\s+/);
                    for (const lid of rgLblIds) {
                        const lblEl = document.getElementById(lid);
                        if (lblEl && lblEl.innerText.trim()) {
                            qLabel = lblEl.innerText.trim();
                            break;
                        }
                    }
                }
                if (!qLabel && rg && rg.getAttribute('aria-label')) {
                    qLabel = rg.getAttribute('aria-label').trim();
                }
                if (rowLabel) {
                    qLabel = qLabel ? `${qLabel} - ${rowLabel}` : rowLabel;
                } else if (!qLabel) {
                    if (!rg && rawOptionName) {
                        qLabel = rawOptionName;
                    } else {
                        qLabel = 'Select an option';
                    }
                }
                
                if (!radioGroups[groupKey]) {
                    radioGroups[groupKey] = {
                        id: injectedId,
                        role: 'radio',
                        label: qLabel,
                        type: 'radio',
                        options: [],
                        option_map: {},
                        required: isRequired,
                        is_matrix: isMatrix,
                        matrix_row: rowLabel,
                        container_key: groupKey
                    };
                }
                if (optText && !radioGroups[groupKey].options.includes(optText)) {
                    radioGroups[groupKey].options.push(optText);
                    radioGroups[groupKey].option_map[optText] = injectedId;
                }
                return;
            }
            
            // 6. Checkbox Group Aggregation:
            // If checkboxes share a container/fieldset/question, aggregate them as a multi-select checkbox group.
            if (role === 'checkbox') {
                const groupParent = el.closest('fieldset, [role="group"], .cf-question, .question, .question-container, .geS5n, .QuestionOuter');
                if (groupParent) {
                    const siblingChecks = groupParent.querySelectorAll('input[type="checkbox"], [role="checkbox"]');
                    if (siblingChecks.length > 1) {
                        const checkKey = getStableContainerKey(groupParent, 'chkgrp');
                        let qLabel = contextHeading;
                        if (!qLabel && groupParent.getAttribute('aria-labelledby')) {
                            const chkIds = groupParent.getAttribute('aria-labelledby').trim().split(/\s+/);
                            for (const lid of chkIds) {
                                const lblEl = document.getElementById(lid);
                                if (lblEl && lblEl.innerText.trim()) {
                                    qLabel = lblEl.innerText.trim();
                                    break;
                                }
                            }
                        }
                        if (!qLabel && groupParent.getAttribute('aria-label')) {
                            qLabel = groupParent.getAttribute('aria-label').trim();
                        }
                        if (!qLabel) {
                            qLabel = 'Select all that apply';
                        }
                        const optText = rawOptionName || el.value || `Option ${index}`;
                        
                        if (!checkGroups[checkKey]) {
                            checkGroups[checkKey] = {
                                id: injectedId,
                                role: 'checkbox',
                                label: qLabel,
                                type: 'checkbox',
                                options: [],
                                option_map: {},
                                required: isRequired,
                                is_matrix: isMatrix,
                                container_key: checkKey
                            };
                        }
                        if (optText && !checkGroups[checkKey].options.includes(optText)) {
                            checkGroups[checkKey].options.push(optText);
                            checkGroups[checkKey].option_map[optText] = injectedId;
                        }
                        return;
                    }
                }
            }
            
            // Clean up label for non-radio / standalone fields
            if (contextHeading) {
                if (!name || /^[\d_\[\]\s-]+$/.test(name) || name.toLowerCase().startsWith('unknown')) {
                    name = contextHeading;
                } else if (contextHeading !== name && !name.startsWith(contextHeading)) {
                    name = contextHeading + " - " + name;
                }
            }
            
            // 7. Dropdown Option Extraction
            let options = [];
            if (tag === 'select') {
                options = Array.from(el.options).map(opt => (opt.text || opt.value || '').trim()).filter(Boolean);
            } else if (role === 'listbox' || role === 'combobox') {
                const opts = el.querySelectorAll('[role="option"], li');
                opts.forEach(opt => {
                    const optText = (opt.innerText || opt.getAttribute('value') || '').trim();
                    if (optText) options.push(optText);
                });
            }
            
            const fieldContKey = container ? getStableContainerKey(container, 'cont') : (tr ? getStableContainerKey(tr, 'row') : '');

            results.push({
                id: injectedId,
                real_id: el.id || '',
                name: el.name || '',
                role: role || 'textbox',
                label: name || `Unknown Field ${index}`,
                type: el.type || (tag === 'select' ? 'select' : 'text'),
                options: options,
                required: isRequired,
                is_matrix: isMatrix,
                matrix_row: rowLabel,
                container_key: fieldContKey
            });
        });
        
        // Append aggregated radio groups
        Object.values(radioGroups).forEach(group => {
            results.push(group);
        });
        
        // Append aggregated checkbox groups
        Object.values(checkGroups).forEach(group => {
            results.push(group);
        });

        // 7. Ranking / Drag-and-Drop Question Detection
        // Universally identifies Qualtrics Simple & Classic Layout (.RO, .Ranking, .ChoiceStructure),
        // pointer-driven handle cards, SurveyMonkey, SurveyJS, and ARIA sortable lists.
        const candidateContainers = document.querySelectorAll(`
            .Ranking,
            .RO,
            .ChoiceStructure.ui-sortable,
            [data-widget="ranking"],
            .rank-order,
            .QuestionOuter.RO,
            .QuestionOuter,
            .question-container,
            .cf-question,
            .question,
            [role="region"],
            fieldset,
            div[class*="ranking" i],
            ul[class*="ranking" i]
        `);
        
        const processedRankContainers = new Set();
        let rankCounter = 0;
        
        candidateContainers.forEach(rc => {
            const containerEl = rc.closest('.QuestionOuter, .cf-question, .question, .question-container, [role="region"], fieldset') || rc;
            if (processedRankContainers.has(containerEl)) return;
            
            const rRect = containerEl.getBoundingClientRect();
            if (rRect.width === 0 && rRect.height === 0) return;
            
            // Extract question prompt
            let qPrompt = "";
            const titleEl = containerEl.querySelector('.QuestionText, [class*="QuestionText" i], .question-title, .question-text, [class*="question-text" i], [class*="prompt" i], [data-kind="field-label"], .cf-question__text, [role="heading"], legend, .title, h1, h2, h3, h4');
            if (titleEl) {
                qPrompt = titleEl.innerText.trim();
            } else {
                const clone = containerEl.cloneNode(true);
                clone.querySelectorAll('.validation-error, .error, .ValidationError, [class*="error" i], [class*="alert" i], [role="alert"]').forEach(e => e.remove());
                const lines = clone.innerText.split('\n').map(l => l.trim()).filter(l => l.length > 2 && !l.startsWith('⊘'));
                qPrompt = lines[0] || "Rank your choices";
            }
            if (!qPrompt) qPrompt = "Rank your choices";
            const promptLower = qPrompt.toLowerCase();
            
            // Check if this container is a genuine ranking question
            const hasExplicitRankClass = containerEl.matches('.Ranking, .RO, [class*="ranking" i], [class*="rank-order" i], [class*="sortable" i], [data-widget="ranking"]') ||
                                         containerEl.querySelector('.Ranking, .RO, [class*="ranking" i], [class*="rank-order" i], [class*="sortable" i], [data-widget="ranking"]') !== null;
            const hasDragHandle = containerEl.querySelector('[class*="handle" i], [class*="grip" i], button[aria-label*="reorder" i], [aria-label*="drag" i], [aria-roledescription*="sort" i]') !== null;
            const hasRankKeyword = ['rank', 'order your', 'top three', 'top 3', 'top two', 'top 2', 'top four', 'top 4', 'priorit', 'arrange', 'preference order'].some(k => promptLower.includes(k));
            
            if (!hasExplicitRankClass && !hasDragHandle && !hasRankKeyword) {
                return;
            }
            
            // Draggable / Ranked item selectors supporting standard and modern headless/custom layouts
            const itemSelectors = [
                'li[draggable="true"]',
                'div[draggable="true"]',
                '.ui-sortable li',
                '.Selection',
                '.rank-item',
                '.draggable-row',
                '.sortable-item',
                'tr[draggable="true"]',
                '[data-choice-id]',
                '[role="listitem"]',
                '.ChoiceStructure > li',
                '.ChoiceStructure > div',
                '.choice-list > div',
                '.rank-order-list > div',
                '[class*="choice-item" i]',
                '[class*="ranking-item" i]',
                'div:has(> [class*="handle" i])',
                'div:has(> [class*="grip" i])',
                'div:has(> button[aria-label*="reorder" i])',
                'li:has([class*="handle" i])'
            ];
            
            let itemEls = Array.from(containerEl.querySelectorAll(itemSelectors.join(', ')));
            if (itemEls.length === 0) {
                const innerList = containerEl.querySelector('ul, ol, [role="list"], .ChoiceStructure, .choice-list, .rank-order-list');
                if (innerList && innerList.children.length > 0) {
                    itemEls = Array.from(innerList.children);
                }
            }
            
            // Filter only visible, unique, valid choice items
            itemEls = itemEls.filter((el, idx, self) => {
                if (el.matches('.handle, .drag-handle, .ui-sortable-handle, [class*="handle" i], svg, button')) return false;
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                const txt = (el.innerText || el.textContent || '').trim();
                return rect.width > 0 && rect.height > 0 &&
                       style.display !== 'none' &&
                       style.visibility !== 'hidden' &&
                       txt.length > 0 &&
                       self.indexOf(el) === idx;
            });
            
            if (itemEls.length === 0) return;
            processedRankContainers.add(containerEl);
            
            const rOptions = [];
            const rOptionMap = {};
            const itemMeta = [];
            
            itemEls.forEach((it, itIdx) => {
                const itId = `stillbot_f${__FRAME_IDX__}_rank_${rankCounter}_${itIdx}`;
                it.setAttribute('data-stillbot-id', itId);
                
                // Attach stable handle targeting if a handle child exists
                const handleEl = it.querySelector('.handle, .drag-handle, .ui-sortable-handle, [class*="handle" i], [class*="grip" i], button[aria-label*="reorder" i], svg');
                if (handleEl) {
                    handleEl.setAttribute('data-stillbot-handle-id', `${itId}_handle`);
                }
                
                let itLabel = "";
                const labelSub = it.querySelector('.Label, [class*="Label" i], .label, .item-text, .rank-label, span:not(.handle):not(.drag-handle)');
                if (labelSub && labelSub.innerText.trim()) {
                    itLabel = labelSub.innerText.trim();
                } else {
                    const clone = it.cloneNode(true);
                    clone.querySelectorAll('.handle, .drag-handle, .ui-sortable-handle, [class*="handle" i], [class*="grip" i], button[aria-label*="reorder" i], svg').forEach(h => h.remove());
                    clone.querySelectorAll('.rank, .rank-number, .rank-pill, [class*="rank" i], .badge').forEach(r => r.remove());
                    itLabel = clone.innerText.trim();
                }
                if (!itLabel) itLabel = it.innerText.trim() || `Choice ${itIdx + 1}`;
                const itClean = itLabel.replace(/^\d+[\.\s\-:]+/, '').trim() || itLabel;
                
                rOptions.push(itClean);
                rOptionMap[itClean] = itId;
                itemMeta.push({
                    id: itId,
                    label: itClean,
                    real_id: it.id || '',
                    index: itIdx
                });
            });
            
            // Check for target dropzone (dual-bucket ranking)
            const dropzoneEl = containerEl.querySelector('.Group.Ranked, .dropzone, [data-widget="dropzone"], ul.Ranked, .target-bucket');
            let targetDropId = "";
            if (dropzoneEl) {
                targetDropId = `stillbot_f${__FRAME_IDX__}_dropzone_${rankCounter}`;
                dropzoneEl.setAttribute('data-stillbot-id', targetDropId);
            }
            
            let isRequired = containerEl.innerText.includes('*') || containerEl.querySelector('.required, .req, [aria-label*="required" i]') !== null;
            
            const contKey = getStableContainerKey(containerEl, 'rank');
            const rankingId = `stillbot_f${__FRAME_IDX__}_rank_q_${rankCounter}`;
            containerEl.setAttribute('data-stillbot-id', rankingId);
            rankCounter++;
            
            // Extract target count from prompt (e.g. "top three", "top 3")
            let targetCount = itemEls.length;
            const topNumMatch = qPrompt.match(/top\s+(\d+|one|two|three|four|five|six|seven|eight|nine|ten)/i);
            if (topNumMatch) {
                const wordMap = { one: 1, two: 2, three: 3, four: 4, five: 5, six: 6, seven: 7, eight: 8, nine: 9, ten: 10 };
                const rawVal = topNumMatch[1].toLowerCase();
                targetCount = wordMap[rawVal] || parseInt(rawVal, 10) || itemEls.length;
            }

            results.push({
                id: rankingId,
                real_id: containerEl.id || '',
                name: qPrompt,
                role: 'ranking',
                label: qPrompt,
                type: 'ranking',
                options: rOptions,
                option_map: rOptionMap,
                required: isRequired,
                items: itemMeta,
                item_elements: itemMeta,
                target_count: targetCount,
                target_drop_id: targetDropId,
                target_container_id: targetDropId,
                is_matrix: false,
                container_key: contKey
            });
        });
        
        return results;
    }
    """
    script_to_run = js_script.replace('__FRAME_IDX__', str(frame_index))
    try:
        fields = await frame.evaluate(script_to_run)
        # Compute stable logical ID and question fingerprints in Python
        for f in fields:
            cont_key = f.get("container_key", "")
            matrix_row = f.get("matrix_row", "")
            f["logical_id"] = compute_logical_question_id(
                frame_index,
                cont_key,
                f.get("role", "textbox"),
                f.get("label", ""),
                matrix_row
            )
            f["fingerprint"] = compute_question_fingerprint(
                frame_index,
                f.get("role", "textbox"),
                f.get("label", ""),
                f.get("options", []),
                container_key=cont_key,
                matrix_row=matrix_row
            )
        return fields
    except Exception as e:
        print(f"Failed to scrape frame {frame_index}: {e}")
        return []

async def scrape_page(page: Page):
    """
    Recursively scrapes all frames on the page and returns all normalized fields.
    """
    print("🕸️ Running Tier 1 Semantic Scraper...")
    all_fields = []
    
    # Wait for DOM/network stabilization
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=3000)
    except:
        pass
        
    # Scrape main frame
    fields = await scrape_frame(page.main_frame, 0)
    all_fields.extend(fields)
    
    # Scrape child frames (iframes)
    for idx, frame in enumerate(page.main_frame.child_frames):
        try:
            f_list = await scrape_frame(frame, idx + 1)
            all_fields.extend(f_list)
        except Exception:
            pass
        
    print(f"✅ Found {len(all_fields)} fields.")
    return all_fields

async def prepare_vision_fallback(page: Page):
    """
    Tier 2 Vision Fallback.
    Triggered when DOM scraping fails. 
    Draws numbered bounding boxes around potential interactive elements and takes a screenshot.
    """
    if not config.ENABLE_VISION_FALLBACK:
        return 0
        
    print("⚠️ Tier 1 failed to find fields. Triggering Tier 2 Vision Fallback...")
    js_script = """
    () => {
        let counter = 1;
        const boxes = [];
        const candidates = document.querySelectorAll('div, span, p, button, a');
        
        candidates.forEach(el => {
            const style = window.getComputedStyle(el);
            const hasBorder = style.borderWidth && style.borderWidth !== '0px' && style.borderStyle !== 'none';
            if (el.isContentEditable || (hasBorder && el.innerText.length < 50 && el.childElementCount === 0 && el.offsetHeight > 15)) {
                if (el.offsetWidth === 0 || el.offsetHeight === 0) return;
                
                const rect = el.getBoundingClientRect();
                const overlay = document.createElement('div');
                overlay.style.position = 'absolute';
                overlay.style.top = `${rect.top + window.scrollY}px`;
                overlay.style.left = `${rect.left + window.scrollX}px`;
                overlay.style.width = `${rect.width}px`;
                overlay.style.height = `${rect.height}px`;
                overlay.style.border = '2px solid red';
                overlay.style.backgroundColor = 'rgba(255,0,0,0.2)';
                overlay.style.zIndex = '999999';
                overlay.style.pointerEvents = 'none';
                
                const label = document.createElement('div');
                label.style.position = 'absolute';
                label.style.top = '0';
                label.style.left = '0';
                label.style.backgroundColor = 'red';
                label.style.color = 'white';
                label.style.fontSize = '12px';
                label.style.padding = '2px';
                label.innerText = counter;
                
                overlay.appendChild(label);
                document.body.appendChild(overlay);
                counter++;
            }
        });
        return counter - 1;
    }
    """
    num_boxes = await page.evaluate(js_script)
    if num_boxes > 0:
        os.makedirs("artifacts", exist_ok=True)
        path = "artifacts/vision_fallback.png"
        await page.screenshot(path=path, full_page=True)
        print(f"📸 Vision Fallback: Detected {num_boxes} potential inputs. Screenshot saved to {path}")
    else:
        print("❌ Vision Fallback failed to find any inputs.")
    return num_boxes

async def detect_blocking_modal(page: Page) -> Dict[str, Any]:
    """
    Detects visible blocking modal dialogs, connection error overlays,
    or backdrops intercepting pointer events.
    Returns:
    {
        "found": bool,
        "type": "network_error" | "validation_error" | "consent" | "unknown",
        "title": str,
        "text": str,
        "buttons": [{"id": ..., "text": ...}],
        "is_backdrop": bool
    }
    """
    js = """
    () => {
        const modalSelectors = [
            '[role="dialog"]',
            '[role="alertdialog"]',
            '[aria-modal="true"]',
            '.portal #dialog',
            '.portal .modal',
            '.portal [role="dialog"]',
            '#dialog.modal',
            '#dialog',
            '.modal.show',
            '.modal.is-active',
            '.modal[style*="display: block"]',
            '.ui-dialog',
            '.QFixModal',
            '.AlertModal',
            'div[class*="modal" i]:not([class*="hidden" i])',
            'div[class*="dialog" i]:not([class*="hidden" i])'
        ];
        
        for (const sel of modalSelectors) {
            const modals = Array.from(document.querySelectorAll(sel));
            for (const m of modals) {
                if (m.tagName.toLowerCase() === 'body' || m.id === 'SurveyEngine' || m.classList.contains('Skin')) continue;
                const rect = m.getBoundingClientRect();
                const style = window.getComputedStyle(m);
                const isVis = (rect.width > 30 && rect.height > 30) &&
                              style.display !== 'none' &&
                              style.visibility !== 'hidden' &&
                              parseFloat(style.opacity || '1') > 0.1;
                if (!isVis) continue;
                
                const text = (m.innerText || '').trim();
                if (!text && rect.width < 100) continue;
                
                const titleEl = m.querySelector('[role="heading"], h1, h2, h3, h4, .modal-title, .dialog-title, [class*="title" i]');
                const title = titleEl ? titleEl.innerText.trim() : '';
                
                const buttons = Array.from(m.querySelectorAll('button, input[type="button"], input[type="submit"], a[role="button"], .btn'))
                    .filter(b => {
                        const bRect = b.getBoundingClientRect();
                        const bStyle = window.getComputedStyle(b);
                        return bRect.width > 0 && bRect.height > 0 && bStyle.display !== 'none' && bStyle.visibility !== 'hidden';
                    })
                    .map((b, idx) => {
                        const btnId = b.getAttribute('data-stillbot-modal-btn') || `stillbot_modal_btn_${idx}`;
                        b.setAttribute('data-stillbot-modal-btn', btnId);
                        return {
                            id: btnId,
                            text: (b.innerText || b.value || b.getAttribute('aria-label') || '').trim()
                        };
                    });
                    
                return {
                    found: true,
                    type: 'modal',
                    title: title,
                    text: text,
                    buttons: buttons,
                    is_backdrop: false
                };
            }
        }
        
        // Backdrop overlay check
        const backdropSelectors = ['.modal-backdrop', '.portal-backdrop', '.ui-widget-overlay', '[class*="backdrop" i]'];
        for (const bSel of backdropSelectors) {
            const bds = Array.from(document.querySelectorAll(bSel));
            for (const bd of bds) {
                const rect = bd.getBoundingClientRect();
                const style = window.getComputedStyle(bd);
                if (rect.width > 200 && rect.height > 200 && style.display !== 'none' && style.visibility !== 'hidden' && parseFloat(style.opacity || '1') > 0.05) {
                    return {
                        found: true,
                        type: 'backdrop',
                        title: '',
                        text: 'Backdrop overlay intercepting pointer events',
                        buttons: [],
                        is_backdrop: true
                    };
                }
            }
        }
        
        return { found: false, type: '', title: '', text: '', buttons: [], is_backdrop: false };
    }
    """
    try:
        modal_info = await page.evaluate(js)
    except Exception:
        return {"found": False, "type": "", "title": "", "text": "", "buttons": [], "is_backdrop": False}
        
    if not modal_info.get("found"):
        return modal_info

    # Classify modal
    combined = (modal_info.get("text", "") + " " + modal_info.get("title", "")).lower()
    
    network_phrases = [
        "connection problem", "network error", "connection error", "unable to connect",
        "failed to connect", "internet connection", "connection lost", "offline",
        "request timed out", "server error", "please try again", "trouble connecting",
        "check your internet", "error communicating", "connecting to server"
    ]
    validation_phrases = [
        "please answer this question", "must answer", "required to answer", "answer all required",
        "is required", "must provide an answer", "please correct the errors", "question requires an answer",
        "select an answer", "continue without answering"
    ]
    consent_phrases = [
        "cookie", "cookies", "privacy policy", "terms of service", "terms of use", "consent"
    ]
    
    if any(p in combined for p in network_phrases):
        modal_info["type"] = "network_error"
    elif any(p in combined for p in validation_phrases):
        modal_info["type"] = "validation_error"
    elif any(p in combined for p in consent_phrases):
        modal_info["type"] = "consent"
    elif modal_info.get("is_backdrop"):
        modal_info["type"] = "backdrop"
    else:
        modal_info["type"] = "unknown"
        
    return modal_info


async def check_page_has_questions(page: Page) -> Tuple[bool, List[str]]:
    """
    Detects whether actual survey questions exist in the DOM (even if input fields
    were not extracted by the semantic scraper, e.g. custom or unparsed widgets).
    """
    js = """
    () => {
        const qSelectors = [
            '.QuestionOuter',
            '.QuestionText',
            '[id^="QID"]',
            '.cf-question',
            '.question',
            '.survey-question',
            '[data-widget="ranking"]',
            '.Ranking',
            '.RO',
            '[class*="question-text" i]',
            '[class*="question__text" i]',
            '.question-container',
            'fieldset'
        ];
        const foundTexts = [];
        for (const sel of qSelectors) {
            const els = Array.from(document.querySelectorAll(sel));
            for (const el of els) {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                if (rect.width > 40 && rect.height > 20 && style.display !== 'none' && style.visibility !== 'hidden') {
                    const textEl = el.querySelector('.QuestionText, [class*="QuestionText" i], .question-title, [data-kind="field-label"], .cf-question__text, [role="heading"], legend') || el;
                    const txt = (textEl.innerText || '').trim();
                    if (txt && txt.length > 5 && !txt.toLowerCase().includes('change survey language') && !foundTexts.includes(txt)) {
                        foundTexts.push(txt.slice(0, 150));
                    }
                }
            }
        }
        return foundTexts;
    }
    """
    try:
        q_texts = await page.evaluate(js)
        return (len(q_texts) > 0, q_texts)
    except Exception:
        return (False, [])

