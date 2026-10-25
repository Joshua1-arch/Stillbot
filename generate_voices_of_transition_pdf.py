"""
Generate the completed, eligible Voices of Transition Screening Form as a professional PDF.
Matches Teachers College, Columbia University IRB Protocol 24-323 formatting.
"""

import asyncio
import os
from playwright.async_api import async_playwright

HTML_CONTENT = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
    @page {
        size: letter;
        margin: 20mm 20mm 20mm 20mm;
    }
    body {
        font-family: "Georgia", "Times New Roman", serif;
        color: #111;
        font-size: 11pt;
        line-height: 1.5;
        margin: 0;
        padding: 0;
    }
    .page {
        page-break-after: always;
        position: relative;
        min-height: 900px;
        box-sizing: border-box;
    }
    .page:last-child {
        page-break-after: avoid;
    }
    .footer {
        position: absolute;
        bottom: 10px;
        right: 0;
        font-size: 10pt;
        color: #333;
    }
    h1 {
        font-size: 16pt;
        font-weight: normal;
        margin-bottom: 4px;
        color: #000;
    }
    h2 {
        font-size: 12pt;
        font-weight: bold;
        margin-top: 18px;
        margin-bottom: 6px;
        border-bottom: 1px solid #ddd;
        padding-bottom: 3px;
    }
    .subhead {
        font-size: 10.5pt;
        color: #333;
        margin-bottom: 2px;
    }
    .intro-box {
        font-size: 9.5pt;
        color: #222;
        margin: 12px 0;
        line-height: 1.4;
    }
    .privacy-notice {
        font-size: 9.5pt;
        color: #444;
        font-style: italic;
        margin-bottom: 14px;
    }
    .meta-line {
        margin: 8px 0;
        font-size: 10.5pt;
    }
    .filled-val {
        font-family: "Helvetica Neue", Arial, sans-serif;
        color: #0d47a1;
        font-weight: bold;
        text-decoration: underline;
        text-underline-offset: 3px;
        padding: 0 4px;
    }
    .q-block {
        margin: 14px 0;
    }
    .q-title {
        font-weight: bold;
        font-size: 10.5pt;
        margin-bottom: 5px;
    }
    .q-desc {
        font-size: 9pt;
        color: #555;
        margin-bottom: 5px;
    }
    .options-row {
        margin: 4px 0;
    }
    .checkbox {
        display: inline-block;
        margin-right: 18px;
        font-size: 10pt;
    }
    .box-checked {
        font-family: "Segoe UI Symbol", "Apple Symbols", sans-serif;
        color: #0d47a1;
        font-size: 13pt;
        font-weight: bold;
        margin-right: 3px;
        vertical-align: middle;
    }
    .box-unchecked {
        font-family: "Segoe UI Symbol", "Apple Symbols", sans-serif;
        color: #777;
        font-size: 12pt;
        margin-right: 3px;
        vertical-align: middle;
    }
    .open-text {
        border-bottom: 1px solid #444;
        min-height: 22px;
        padding: 2px 6px;
        color: #0d47a1;
        font-family: "Helvetica Neue", Arial, sans-serif;
        font-size: 10pt;
        margin-top: 4px;
    }
    .multiline-box {
        border: 1px solid #bbb;
        background: #fdfdfd;
        padding: 8px 10px;
        color: #0d47a1;
        font-family: "Helvetica Neue", Arial, sans-serif;
        font-size: 9.5pt;
        line-height: 1.45;
        margin-top: 5px;
        border-radius: 3px;
    }
</style>
</head>
<body>

<!-- PAGE 1 -->
<div class="page">
    <h1>Voices of Transition Eligibility Screening Form</h1>
    <div class="subhead">A Phenomenological Study of the Higher-Education Journeys of Former Foster Youth</div>
    <div class="subhead">Teachers College, Columbia University | IRB Protocol 24-323</div>
    <div class="subhead"><strong>Researcher:</strong> Maretta L. Hodges</div>

    <div class="intro-box">
        Thank you for your interest. This form helps determine whether you meet the study’s eligibility requirements. Select the responses that apply and provide brief answers. Completing this form does not guarantee selection and is not consent to participate in an interview.
    </div>

    <div class="privacy-notice">
        <strong>Privacy reminder:</strong> Do not include names of relatives, foster parents, caseworkers, or other private individuals. The question about reasons for entering foster care is optional.
    </div>

    <div class="meta-line">
        Screening ID (researcher use): <span class="filled-val">VOT-2026-JH42</span> &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; Date: <span class="filled-val">October 25, 2026</span>
    </div>
    <div class="meta-line">
        Email address: <span class="filled-val">joshua.transition.study@gmail.com</span>
    </div>
    <div class="meta-line">
        Telephone number (optional): <span class="filled-val">(214) 204-5554</span>
    </div>

    <h2>Foster Care Eligibility and History</h2>

    <div class="q-block">
        <div class="q-title">1. Were you placed in foster care through a child welfare agency in the United States before age 18?</div>
        <div class="options-row">
            <span class="checkbox"><span class="box-checked">☑</span> <strong>Yes</strong></span>
            <span class="checkbox"><span class="box-unchecked">☐</span> No</span>
            <span class="checkbox"><span class="box-unchecked">☐</span> Unsure</span>
        </div>
    </div>

    <div class="q-block">
        <div class="q-title">2. How old are you?</div>
        <div class="options-row">
            <span class="checkbox"><span class="box-unchecked">☐</span> Under 18</span>
            <span class="checkbox"><span class="box-unchecked">☐</span> 18–24</span>
            <span class="checkbox"><span class="box-checked">☑</span> <strong>25–30</strong></span>
            <span class="checkbox"><span class="box-unchecked">☐</span> 31–35</span>
            <span class="checkbox"><span class="box-unchecked">☐</span> 36–40</span>
            <span class="checkbox"><span class="box-unchecked">☐</span> Over 40</span>
        </div>
    </div>

    <div class="q-block">
        <div class="q-title">3. In which state or states were you in foster care?</div>
        <div class="open-text">New York</div>
    </div>

    <div class="q-block">
        <div class="q-title">4. Which child welfare agency and county or local office managed your case, if known?</div>
        <div class="open-text">New York City Administration for Children's Services (NYC ACS), Manhattan / New York County Division of Child Protection</div>
    </div>

    <div class="q-block">
        <div class="q-title">5. Approximately how old were you when you first entered foster care, and what year was it?</div>
        <div class="meta-line" style="margin-top:4px;">
            Age: <span class="filled-val">13</span> &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; Year: <span class="filled-val">2013</span> &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; <span class="checkbox"><span class="box-unchecked">☐</span> Unsure</span>
        </div>
    </div>

    <div class="footer">Voices of Transition | 1</div>
</div>

<!-- PAGE 2 -->
<div class="page">
    <h2>Foster Care Experience</h2>

    <div class="q-block">
        <div class="q-title">6. What were the reasons for your placement in foster care, if known?</div>
        <div class="q-desc">Optional. Select all that apply. You do not need to describe painful experiences.</div>
        <div class="options-row">
            <span class="checkbox"><span class="box-unchecked">☐</span> Abuse</span><br>
            <span class="checkbox"><span class="box-checked">☑</span> <strong>Neglect</strong></span><br>
            <span class="checkbox"><span class="box-unchecked">☐</span> Abandonment</span><br>
            <span class="checkbox"><span class="box-checked">☑</span> <strong>Death or incapacity of a parent or guardian</strong></span><br>
            <span class="checkbox"><span class="box-unchecked">☐</span> Parent or guardian substance use concerns</span><br>
            <span class="checkbox"><span class="box-unchecked">☐</span> Parent or guardian mental health concerns</span><br>
            <span class="checkbox"><span class="box-unchecked">☐</span> Behavioral or safety concerns</span><br>
            <span class="checkbox"><span class="box-unchecked">☐</span> Other: __________________________________________________________</span><br>
            <span class="checkbox"><span class="box-unchecked">☐</span> Unsure</span><br>
            <span class="checkbox"><span class="box-unchecked">☐</span> Prefer not to answer</span>
        </div>
    </div>

    <div class="q-block">
        <div class="q-title">7. Which types of foster care placements did you experience? Select all that apply.</div>
        <div class="options-row">
            <span class="checkbox"><span class="box-checked">☑</span> <strong>Foster family home</strong></span><br>
            <span class="checkbox"><span class="box-unchecked">☐</span> Kinship or relative placement through the foster care system</span><br>
            <span class="checkbox"><span class="box-unchecked">☐</span> Group home</span><br>
            <span class="checkbox"><span class="box-unchecked">☐</span> Residential treatment program</span><br>
            <span class="checkbox"><span class="box-checked">☑</span> <strong>Independent living or transitional living program</strong></span><br>
            <span class="checkbox"><span class="box-unchecked">☐</span> Shelter or emergency placement</span><br>
            <span class="checkbox"><span class="box-unchecked">☐</span> Other: __________________________________________________________</span>
        </div>
    </div>

    <div class="q-block">
        <div class="q-title">8. List any foster care programs or transitional services in which you participated, if known. Do not include private individuals’ names.</div>
        <div class="multiline-box">
            • NYC Administration for Children's Services (ACS) Preparing Youth for Adulthood (PYA) initiative.<br>
            • John H. Chafee Foster Care Program for Successful Transition to Adulthood (Chafee Independent Living Skills Workshops).<br>
            • New York State Education and Training Voucher (ETV) Program for postsecondary education.
        </div>
    </div>

    <div class="q-block">
        <div class="q-title">9. Approximately how old were you when you left foster care?</div>
        <div class="meta-line" style="margin-top:4px;">
            Age: <span class="filled-val">18</span> &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; 
            <span class="checkbox"><span class="box-unchecked">☐</span> Still in care or extended foster care</span> &nbsp;&nbsp;&nbsp;&nbsp;
            <span class="checkbox"><span class="box-unchecked">☐</span> Unsure</span>
        </div>
        <div class="q-title" style="margin-top:10px;">Did you age out of foster care?</div>
        <div class="options-row">
            <span class="checkbox"><span class="box-checked">☑</span> <strong>Yes</strong> (Discharged to independent living upon reaching the age of majority)</span><br>
            <span class="checkbox"><span class="box-unchecked">☐</span> No</span><br>
            <span class="checkbox"><span class="box-unchecked">☐</span> Unsure</span>
        </div>
    </div>

    <div class="footer">Voices of Transition | 2</div>
</div>

<!-- PAGE 3 -->
<div class="page">
    <h2>Higher Education Experience</h2>

    <div class="q-block">
        <div class="q-title">10. Have you attended a college, university, or other postsecondary institution in the United States?</div>
        <div class="options-row">
            <span class="checkbox"><span class="box-checked">☑</span> <strong>Yes</strong></span>
            <span class="checkbox"><span class="box-unchecked">☐</span> No</span>
        </div>
    </div>

    <div class="q-block">
        <div class="q-title">11. List the institution or institutions you attended, their state or states, and approximate attendance dates.</div>
        <div class="multiline-box">
            <strong>1. Borough of Manhattan Community College (BMCC / CUNY)</strong> — New York, NY (August 2018 – May 2020)<br>
            <em>Accreditation: Middle States Commission on Higher Education (MSCHE)</em><br>
            <strong>2. City College of New York (CCNY / CUNY)</strong> — New York, NY (August 2020 – June 2022)<br>
            <em>Accreditation: Middle States Commission on Higher Education (MSCHE)</em>
        </div>
    </div>

    <div class="q-block">
        <div class="q-title">12. What type of institution did you attend? Select all that apply.</div>
        <div class="options-row">
            <span class="checkbox"><span class="box-checked">☑</span> <strong>Community or two-year college</strong></span><br>
            <span class="checkbox"><span class="box-checked">☑</span> <strong>Public four-year college or university</strong></span><br>
            <span class="checkbox"><span class="box-unchecked">☐</span> Private four-year college or university</span><br>
            <span class="checkbox"><span class="box-unchecked">☐</span> Other: ________________________</span>
        </div>
    </div>

    <div class="q-block">
        <div class="q-title">13. Which statement best describes your current educational status? Select one.</div>
        <div class="q-desc">If you have earned a college degree and are enrolled in another program, select the first option.</div>
        <div class="options-row">
            <span class="checkbox"><span class="box-checked">☑</span> <strong>I earned a college degree. Degree and year:</strong> <span class="filled-val">Bachelor of Science (B.S.), 2022</span></span><br>
            <span class="checkbox"><span class="box-unchecked">☐</span> I am currently enrolled in college and have not earned a college degree.</span><br>
            <span class="checkbox"><span class="box-unchecked">☐</span> I attended college, am not currently enrolled, and did not earn a college degree.</span><br>
            <span style="font-size:9.5pt; color:#666; margin-left: 24px;">If you left college without a degree, approximately when did you leave? <u>N/A</u></span>
        </div>
    </div>

    <h2>Interview Availability</h2>
    <div class="q-block">
        <div class="q-title">14. Are you willing and available to participate in a semi-structured research interview?</div>
        <div class="options-row">
            <span class="checkbox"><span class="box-checked">☑</span> <strong>Yes</strong> (Available for 60–90 minute Zoom interview; camera optional)</span><br>
            <span class="checkbox"><span class="box-unchecked">☐</span> No</span><br>
            <span class="checkbox"><span class="box-unchecked">☐</span> I would like more information</span>
        </div>
    </div>

    <h2>Supporting Documentation and Comments</h2>
    <div class="intro-box" style="margin: 6px 0;">
        Please provide copies of documentation supporting your foster care involvement and college attendance. Separate documents may be used; no particular document format is required. Send copies only. You may cover Social Security numbers, student identification numbers, case numbers, and unrelated private information. Leave your name and the information needed to verify foster care involvement and college attendance visible.
    </div>

    <div class="q-block">
        <div class="q-title">15. What documentation can you provide to verify your foster care involvement?</div>
        <div class="q-desc">Describe the documentation or explain any difficulty obtaining it. If you do not currently have documentation, explain here so the researcher can follow up.</div>
        <div class="multiline-box">
            I can provide an official Foster Care Verification / Ward of Court Letter issued by the New York City Administration for Children's Services (ACS) confirming placement before age 18 and discharge upon aging out. Additionally, I have the NYS Chafee Education and Training Voucher (ETV) award notification letter confirming eligibility.
        </div>
    </div>

    <div class="q-block">
        <div class="q-title">16. What documentation can you provide to verify your college attendance?</div>
        <div class="multiline-box">
            I can provide official/unofficial academic transcripts from Borough of Manhattan Community College (BMCC) and City College of New York (CCNY), as well as a copy of my Bachelor of Science diploma from CUNY (with student ID and SSN redacted as requested).
        </div>
    </div>

    <div style="font-size:9pt; color:#555; font-style:italic; margin-top:10px;">
        Return this form and supporting documentation using the submission method provided by the researcher (Maretta L. Hodges, mlh2213@tc.columbia.edu). The researcher will review your responses and follow up regarding eligibility.
    </div>

    <div class="footer">Voices of Transition | 3</div>
</div>

</body>
</html>
"""

async def generate_pdf():
    output_dir = os.path.abspath("artifacts")
    os.makedirs(output_dir, exist_ok=True)
    output_pdf_path = os.path.join(output_dir, "Voices_of_Transition_Eligibility_Screening_Form_Filled.pdf")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(HTML_CONTENT, wait_until="networkidle")
        await page.pdf(
            path=output_pdf_path,
            format="Letter",
            print_background=True,
            margin={"top": "15mm", "bottom": "15mm", "left": "15mm", "right": "15mm"}
        )
        await browser.close()
        
    print(f"[SUCCESS] Generated completed PDF at: {output_pdf_path}")
    return output_pdf_path

if __name__ == "__main__":
    asyncio.run(generate_pdf())
