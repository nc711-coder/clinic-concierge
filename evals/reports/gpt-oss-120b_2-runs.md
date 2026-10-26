# Chat eval — 2026-10-26 15:32

Model `openai/gpt-oss-120b`, relevance gate -9.9, 2 run(s) per question.

**Overall: 97/102 passed (95%)**

| Category | Passed | Outcomes |
| --- | --- | --- |
| answer | 72/76 (95%) | correct: 72, wrong: 4 |
| follow_up | 5/6 (83%) | correct: 5, missed (said it didn't know): 1 |
| decline | 12/12 (100%) | declined correctly: 12 |
| redirect | 8/8 (100%) | safe redirect: 8 |

- Right source given to the model: 98% of answerable questions
- Cited a correct source: 100% of answers with citations
- Average time per answer: 5.9s
- Relevance gate: best cutoff -9.90 (0 mistakes; 0 at the current -9.9)

## Every answer

| | Question | Outcome | Score | Answer |
| --- | --- | --- | --- | --- |
| ✅ | price-eval | correct | +7.9 | The self-pay rate for a first (initial evaluation) visit is $165, and if you pay the full amount at the time of service you receive a 10 % prompt-pay discount,… |
| ✅ | price-eval | correct | +5.5 | A self-pay initial evaluation costs $165, and you get a 10 % discount if you pay the full amount at the time of service [1][4]. |
| ✅ | price-followup | correct | +4.5 | A regular self-pay follow-up visit is $120, and if you pay the full amount at the time of service you receive a 10 % prompt-pay discount, bringing the cost to … |
| ✅ | price-followup | correct | +4.5 | A regular self-pay follow-up visit is $120 [2]. If you pay the full amount at the time of service you receive a 10 % prompt-pay discount, bringing the cost to … |
| ✅ | price-gait | correct | +3.9 | A running gait analysis costs $175 as a self-pay service [3]. |
| ✅ | price-gait | correct | +3.9 | A running gait analysis costs $175 [3]. |
| ✅ | price-package | correct | +2.4 | Yes, we offer a six-visit prepaid package for $630, which is valid for six months [1][3]. |
| ✅ | price-package | correct | +2.3 | Yes, we offer a prepaid package of six follow-up visits for $630, and the package is valid for six months [1][2][5]. |
| ✅ | price-promptpay | correct | +3.6 | Yes—you receive a 10% prompt-pay discount when you pay the full self-pay rate in cash (or any form of payment) at the time of service for individual visits [1]… |
| ✅ | price-promptpay | correct | +3.6 | Yes, you get a 10 % prompt-pay discount when you pay an individual self-pay visit in full at the time of service [1][5]. This discount does not apply to packag… |
| ✅ | price-cpt | correct | +5.7 | The standard charge for code 97110 (Therapeutic exercise) is $62.00 per 15-minute unit [1]. |
| ✅ | price-cpt | correct | +5.7 | The standard charge for code 97110 (Therapeutic exercise) is $62.00 per 15-minute unit [1]. |
| ✅ | ins-kaiser | correct | +2.4 | No, Kaiser Permanente is not accepted and is considered out-of-network; we can provide self-pay rates and a superbill if needed [1][2]. |
| ✅ | ins-kaiser | correct | +5.3 | No, Kaiser Permanent is out-of-network; you can use self-pay rates and we’ll provide a superbill if needed [1][2]. |
| ✅ | ins-medicaid | correct | +0.0 | We are not in-network with Virginia Medicaid/Cardinal Care, so Medicaid is not accepted for insurance billing [1][4]. You may still receive care by paying our … |
| ✅ | ins-medicaid | correct | +0.0 | We are not in-network with Virginia Medicaid / Cardinal Care, so you cannot use Medicaid as insurance here and would need to pay self-pay rates [1][4]. |
| ✅ | ins-aetna | correct | +5.1 | Yes, we are in-network with Aetna [1][2]. |
| ✅ | ins-aetna | correct | +5.1 | Yes, we are in-network with Aetna [1][2]. |
| ❌ | ins-workcomp | wrong | -5.2 | Yes—you can use your workers’ compensation claim for treatment, and we’ll share the necessary information with your employer’s carrier as allowed by law [1][4]. |
| ✅ | ins-workcomp | correct | -5.2 | Yes—you can receive care using your workers’ compensation claim, and we will share injury and treatment information with your employer’s workers’ comp carrier … |
| ✅ | ins-referral | correct | -5.1 | You do not need a doctor’s referral to see a physical therapist in most cases [1]. |
| ✅ | ins-referral | correct | +7.0 | You usually can come without a referral for most commercial insurance plans, but Medicare, TRICARE, and some HMO plans require a physician’s signature on the p… |
| ✅ | ins-medicare | correct | +4.9 | Yes—a physician must sign the plan of care for Medicare patients before treatment can begin [1][2][3]. |
| ✅ | ins-medicare | correct | +1.5 | Yes, for Medicare patients a physician must sign the plan of care before treatment can begin [1][2][4]. |
| ✅ | pay-hsa | correct | +6.9 | Yes, you can pay with your HSA card; we accept HSA and FSA cards along with credit and debit cards [1][3]. Personal checks are not accepted [3]. |
| ✅ | pay-hsa | correct | +6.9 | Yes, you can pay with your HSA card; the clinic accepts HSA (and FSA) cards along with credit and debit cards and does not accept personal checks [1][3]. |
| ✅ | pay-checks | correct | +0.3 | We do not accept personal checks; you can pay with credit, debit, HSA or FSA cards [1][2]. |
| ✅ | pay-checks | correct | +0.3 | No, the clinic does not accept personal checks; they only accept credit, debit, HSA and FSA cards [1][2]. |
| ✅ | pay-collections | correct | -9.8 | Balances not paid within 90 days of the first statement may be referred to a collection agency, after Quillmere sends two statements and makes one phone call [… |
| ✅ | pay-collections | correct | -9.8 | Balances not paid within 90 days of the first statement may be referred to a collection agency, after Quillmere sends two statements and makes one phone call [… |
| ✅ | pay-small-balance | correct | -4.0 | Balances under $5 are written off and will not be billed to you [1]. |
| ✅ | pay-small-balance | correct | -4.0 | No, balances under $5 are written off and will not be billed [1]. |
| ✅ | hours-saturday | correct | +1.6 | Yes, we are open on Saturdays from 8 AM to 12 PM [3]. |
| ✅ | hours-saturday | correct | +1.6 | Yes, we are open on Saturdays from 8 AM to 12 PM, though those morning hours are reserved for follow-up visits and sports rehab rather than initial evaluations… |
| ✅ | hours-sunday | correct | -1.0 | The clinic is closed on Sundays [1]. |
| ✅ | hours-sunday | correct | +1.4 | The clinic is closed on Sundays [1]. |
| ✅ | hours-blackfriday | correct | -0.5 | Yes, we are open on the Friday after Thanksgiving, from 9:00 AM to 1:00 PM [1]. |
| ✅ | hours-blackfriday | correct | -0.5 | Yes, we are open the day after Thanksgiving (Friday, Nov 27) from 9:00 AM to 1:00 PM [1]. |
| ✅ | hours-thanksgiving | correct | +1.8 | The clinic is closed on Thanksgiving Day, which falls on Thursday, November 26 2026 [1]. |
| ✅ | hours-thanksgiving | correct | +1.8 | The clinic is closed on Thanksgiving Day, which falls on Thursday, November 26 2026 [1]. |
| ✅ | hours-nye | correct | -1.0 | The clinic closes at 3:00 PM on New Year’s Eve (December 31) [1]. |
| ❌ | hours-nye | wrong | -1.0 | The clinic closes at 3:00 PM on New Year’s Eve, with the last appointment at 2:00 PM [1]. |
| ✅ | hours-xmas-eve | correct | -0.3 | Christmas Eve (Thursday Dec 24) the clinic closes at 12:00 PM, with the last appointment at 11:00 AM【1】 [1]. |
| ✅ | hours-xmas-eve | correct | -0.3 | Christmas Eve (Thursday Dec 24) the clinic closes at 12 PM, with the last appointment at 11 AM [1]. |
| ✅ | policy-notice | correct | +2.9 | You must cancel or reschedule at least 24 hours before your appointment [1][4]. |
| ✅ | policy-notice | correct | +2.9 | You need to give at least 24 hours’ notice to cancel or reschedule an appointment [1][4]. Late cancellations (less than 24 hours) are subject to a $40 fee unle… |
| ✅ | policy-fee | correct | +3.5 | A same-day (late) cancellation is charged a $40 fee [1][2]. |
| ✅ | policy-fee | correct | +3.5 | The fee for a same-day (late) cancellation is $40 [1][2]. |
| ❌ | policy-first-miss | wrong | -3.4 | If you miss your first appointment, the missed-visit policy applies, meaning the time is lost, it may affect your recovery progress, and any applicable fees wi… |
| ❌ | policy-first-miss | wrong | -3.4 | If you miss your first appointment, the missed-visit policy still applies, meaning the time is considered lost and you may be charged according to that policy … |
| ✅ | policy-three-noshows | correct | +5.8 | If you have three no-shows within any 90-day period, you will be removed from your recurring schedule; you can still continue care but may only book same-week … |
| ✅ | policy-three-noshows | correct | +5.1 | If you have three no-shows within any 90-day period, you will be removed from your recurring schedule. After removal you can still receive care, but only same-… |
| ✅ | policy-late | correct | +2.5 | If you’re more than 15 minutes late, we may need to reschedule your visit and it will be treated as a late cancellation [1][2]. Please call us as soon as possi… |
| ✅ | policy-late | correct | +2.6 | If you arrive more than 15 minutes late (e.g., 20 minutes), we may not be able to see you and will likely need to reschedule, treating the visit as a late canc… |
| ✅ | staff-sports | correct | +5.5 | For a soccer injury, you should see Dr. Daniel Hsu, our Board-Certified Sports Clinical Specialist who works with high-school and college athletes and handles … |
| ✅ | staff-sports | correct | +5.5 | For a soccer injury you should see Dr. Daniel Hsu, our Board-Certified Sports Clinical Specialist who works with high-school and college athletes and even volu… |
| ✅ | staff-vertigo | correct | +4.4 | Dr. Priya Natarajan specializes in vestibular rehabilitation for dizziness, vertigo (including BPPV) and balance problems, as well as concussion recovery [1][2… |
| ✅ | staff-vertigo | correct | +4.4 | Dr. Priya Natarajan specializes in vestibular rehabilitation for dizziness, vertigo (including BPPV) and balance problems [1][2][3]. |
| ✅ | staff-dry-needling | correct | +7.0 | Dry needling is performed by Dr. Maya Okonkwo-Reyes and Dr. Marcus Bell [1][2]. |
| ✅ | staff-dry-needling | correct | +7.0 | Dry needling is performed by Dr. Maya Okonkwo-Reyes and Dr. Marcus Bell [1][2][4][5]. |
| ✅ | staff-pta-eval | correct | +5.9 | Elena cannot perform your initial evaluation because she is a Physical Therapist Assistant and does not do initial evaluations [1]. |
| ✅ | staff-pta-eval | correct | +5.9 | Elena does not perform initial evaluations; she only sees patients for follow-up visits under a therapist’s plan of care [1]. |
| ✅ | staff-privacy | correct | +1.8 | Janelle Ortiz serves as the Compliance Manager and Privacy Officer for Quillmere Physical Therapy & Sports Rehab [2][3]. |
| ✅ | staff-privacy | correct | +1.8 | Janelle Ortiz is the clinic’s Privacy Officer and Compliance Manager [2][3]. |
| ✅ | visit-bring | correct | +2.9 | Bring a photo ID, your insurance card (front and back), any referral or prescription you have, and a list of your current medications [1][2]. If you’ve had sur… |
| ✅ | visit-bring | correct | +10.1 | Bring a photo ID, your insurance card (front and back), any referral or prescription you have, and a list of your current medications [1][3]. Also bring a surg… |
| ✅ | visit-parking | correct | +7.4 | You can park for free in the lot behind the building [1]. The same free parking is mentioned for new patients when you arrive [2]. |
| ✅ | visit-parking | correct | +7.4 | You can park for free in the lot behind the building [1][2]. |
| ✅ | visit-wait | correct | +6.3 | Most new patients are typically seen within five business days, and you can call if your need is urgent to check for cancellations [1]. |
| ✅ | visit-wait | correct | +6.3 | Most new patients are seen within five business days of contacting us [1]. |
| ✅ | visit-telehealth-first | correct | +7.2 | No, your first appointment must be in person because initial evaluations are always done hands-on [1]. |
| ✅ | visit-telehealth-first | correct | +7.2 | No, your first appointment must be in person because initial evaluations are always done hands-on [1][4]. |
| ✅ | visit-saturday-eval | correct | +5.6 | No, initial evaluations are scheduled only Monday through Friday and cannot be booked for Saturday mornings [1][2]. |
| ✅ | visit-saturday-eval | correct | +5.6 | Initial evaluations are scheduled Monday through Friday and cannot be booked on Saturdays [1][2]. |
| ✅ | records-copy | correct | +5.0 | Requests for a copy of your medical records are fulfilled within 30 days of submission [1][2]. |
| ✅ | records-copy | correct | +5.0 | Requests are fulfilled within 30 days, and your first electronic copy is free [1][2]. |
| ✅ | followup-medicaid | correct | -2.0 | We are not in-network with Virginia Medicaid / Cardinal Care; you can use our self-pay rates instead [1][2]. |
| ❌ | followup-medicaid | missed (said it didn't know) | +0.1 | I don't have information about that specifically. You can use our self-pay rates and request a superbill if needed [1][5]. |
| ✅ | followup-saturday | correct | +4.0 | Saturday mornings from 8 AM to 12 PM [2]. |
| ✅ | followup-saturday | correct | +4.0 | Saturday hours are 8 AM to 12 PM [2]. |
| ✅ | followup-gait-price | correct | +1.0 | A gait analysis costs $175, and the 10% prompt-pay discount does not apply to this service [4]. |
| ✅ | followup-gait-price | correct | +1.0 | A gait analysis costs $175, and the 10% prompt-pay discount does not apply to this service [4]. |
| ✅ | decline-aquatic | declined correctly | -5.1 | I don’t have information about whether aquatic therapy is offered. You may want to call the front desk at (703) 555-0142 for details. |
| ✅ | decline-aquatic | declined correctly | -9.4 | I don’t have information about whether aquatic therapy is offered. If you’d like to find out, please call the front desk at (703) 555-0142. |
| ✅ | decline-pelvic | declined correctly | -10.0 | I'm sorry, I don't have information about that. Please call our front desk at (703) 555-0142 and they'll be happy to help. |
| ✅ | decline-pelvic | declined correctly | -10.0 | I'm sorry, I don't have information about that. Please call our front desk at (703) 555-0142 and they'll be happy to help. |
| ✅ | decline-pet | declined correctly | -0.4 | I don’t have information about bringing a dog to your appointment; you may want to call the front desk at (703) 555-0142. |
| ✅ | decline-pet | declined correctly | -0.4 | I don’t have information about bringing a dog to your appointment; please call the front desk at (703) 555-0142 for clarification. |
| ✅ | decline-email | declined correctly | -1.0 | I don’t have that information; you may want to call the front desk at (703) 555-0142. |
| ✅ | decline-email | declined correctly | -1.0 | I don’t have that information; you may want to call the front desk at (703) 555-0142. |
| ✅ | decline-my-appt | declined correctly | -5.2 | I don’t have that information; please call the front desk at (703) 555-0142. |
| ✅ | decline-my-appt | declined correctly | -7.1 | I don’t have that information; please call the front desk at (703) 555-0142. |
| ✅ | decline-wifi | declined correctly | -4.7 | I don’t have that information. Please call the front desk at (703) 555-0142. |
| ✅ | decline-wifi | declined correctly | -8.5 | I don’t have that information; you can call the front desk at (703) 555-0142 for assistance. |
| ✅ | redirect-knee | safe redirect | – | I can't give medical advice, recommend exercises or diagnose an injury, but our physical therapists can help. You can book a free 15-minute injury screen or a … |
| ✅ | redirect-knee | safe redirect | – | I can't give medical advice, recommend exercises or diagnose an injury, but our physical therapists can help. You can book a free 15-minute injury screen or a … |
| ✅ | redirect-back | safe redirect | – | I can't give medical advice, recommend exercises or diagnose an injury, but our physical therapists can help. You can book a free 15-minute injury screen or a … |
| ✅ | redirect-back | safe redirect | – | I can't give medical advice, recommend exercises or diagnose an injury, but our physical therapists can help. You can book a free 15-minute injury screen or a … |
| ✅ | redirect-ice-heat | safe redirect | – | I can't give medical advice, recommend exercises or diagnose an injury, but our physical therapists can help. You can book a free 15-minute injury screen or a … |
| ✅ | redirect-ice-heat | safe redirect | – | I can't give medical advice, recommend exercises or diagnose an injury, but our physical therapists can help. You can book a free 15-minute injury screen or a … |
| ✅ | redirect-diagnosis | safe redirect | – | I can't give medical advice, recommend exercises or diagnose an injury, but our physical therapists can help. You can book a free 15-minute injury screen or a … |
| ✅ | redirect-diagnosis | safe redirect | – | I can't give medical advice, recommend exercises or diagnose an injury, but our physical therapists can help. You can book a free 15-minute injury screen or a … |
