---
license: apache-2.0
---

# Artificial Analysis Long Context Reasoning (AA-LCR) Dataset

AA-LCR includes 100 hard text-based questions that require reasoning across multiple real-world documents, with each document set averaging ~100k input tokens. Questions are designed such that answers cannot be directly retrieved from documents and must instead be reasoned from multiple information sources.

## Dataset Development

AA-LCR was created through a rigorous multi-phase process involving several members of the Artificial Analysis research team and more than a dozen undergraduate students who were engaged on a short-term contract basis to write and/or validate questions. 

**Document Curation**: We selected diverse document sets (company reports, government consultations, legal documents, academic papers) averaging ~100,000 tokens each, representing real materials knowledge workers analyze.

**Question Creation**: Undergraduate students from various disciplines developed questions with access via a dataset development dashboard to non-frontier test models to validate question difficulty (GPT-4o-mini, Llama-3.1-70B, Gemini 1.5 Flash). These models were specifically chosen to give creators a sense of AI capabilities without access to frontier models, preventing adversarial selection against particular frontier models. Creators were instructed to develop practical questions requiring multi-document reasoning, and to ensure that the questions were sufficiently hard for the above models to fail to get them right.

**Human Validation**: Every question was verified through human testing:

- Evaluators answered questions using the same document sets provided to AI models
- Human performance revealed the benchmark's challenging nature - individual evaluators achieved modest accuracy rates, typically answering 40-60% of questions correctly on the first attempt
- However, when presented with correct answers, evaluators showed high agreement confirming question validity and demonstrating that while difficult, the questions had clear, defensible answers
- Questions failing verification were revised or discarded
- Every question in AA-LCR was answered correctly by at least one human tester, ensuring all questions have verified solutions

This approach validates that AA-LCR tests genuine reasoning capabilities rather than obscure knowledge, while acknowledging the inherent difficulty of long context reasoning tasks even for human experts.

## Technical Details

AA-LCR comprises 100 questions across 7 types of text-only documents (i.e. Company Reports, Industry Reports, Government Consultations, Academia, Legal, Marketing Materials and Survey Reports). Multiple independent documents, forming a Document Set with a total length of ~100k tokens are passed as context for each question. For instance, the Company Documents topic includes separate document sets containing 2023 and 2024 company reports, respectively. 

Each question requires using the Document Set and applying general and mathematical reasoning. 

<div class="overflow-x-auto my-6">
  <table class="min-w-full border border-gray-300 bg-white">
    <thead class="bg-gray-50">
      <tr>
        <th class="border border-gray-300 px-4 py-3 text-left text-sm font-semibold text-gray-900">Parent Category</th>
        <th class="border border-gray-300 px-4 py-3 text-left text-sm font-semibold text-gray-900">Total Questions</th>
        <th class="border border-gray-300 px-4 py-3 text-left text-sm font-semibold text-gray-900">Total Document Sets</th>
        <th class="border border-gray-300 px-4 py-3 text-left text-sm font-semibold text-gray-900">Total Documents</th>
        <th class="border border-gray-300 px-4 py-3 text-left text-sm font-semibold text-gray-900">Total Tokens</th>
        <th class="border border-gray-300 px-4 py-3 text-left text-sm font-semibold text-gray-900">Average Token Per Document Set</th>
      </tr>
    </thead>
    <tbody class="divide-y divide-gray-200">
      <tr class="hover:bg-gray-50">
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900">Company Documents</td>
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900">63</td>
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900">16</td>
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900">92</td>
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900">1,476,239</td>
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900">92,265</td>
      </tr>
      <tr class="hover:bg-gray-50">
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900">Industry Reports</td>
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900">8</td>
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900">4</td>
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900">18</td>
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900">410,698</td>
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900">102,675</td>
      </tr>
      <tr class="hover:bg-gray-50">
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900">Government Consultations</td>
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900">11</td>
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900">3</td>
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900">60</td>
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900">325,254</td>
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900">108,418</td>
      </tr>
      <tr class="hover:bg-gray-50">
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900">Academia</td>
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900">5</td>
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900">2</td>
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900">14</td>
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900">223,776</td>
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900">111,888</td>
      </tr>
      <tr class="hover:bg-gray-50">
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900">Legal</td>
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900">6</td>
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900">2</td>
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900">23</td>
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900">233,050</td>
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900">116,525</td>
      </tr>
      <tr class="hover:bg-gray-50">
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900">Marketing</td>
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900">6</td>
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900">2</td>
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900">16</td>
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900">217,694</td>
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900">108,847</td>
      </tr>
      <tr class="hover:bg-gray-50">
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900">Survey Reports</td>
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900">1</td>
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900">1</td>
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900">11</td>
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900">93,046</td>
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900">93,046</td>
      </tr>
      <tr class="bg-gray-100 font-semibold">
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900 font-bold">Full Dataset</td>
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900 font-bold">100</td>
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900 font-bold">30</td>
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900 font-bold">234</td>
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900 font-bold">2,979,757</td>
        <td class="border border-gray-300 px-4 py-3 text-sm text-gray-900 font-bold">99,325</td>
      </tr>
    </tbody>
  </table>
</div>

**Sample Question:**

\`\`\`json
For the company and quarter where the company reported a 13.5% decline on the prior quarters operating income. What was their adjusted EBITDA? List the company name and adjusted EBITDA

Answer: Equinix, $901 million
\`\`\`

Examples of other types of questions include:

- **Financial Analysis and Comparative Metrics:** Extract financial data and calculate performance metrics
- **Legal and Regulatory Interpretation**: Identify cases/policies under exclusion rules, interpret outcomes and applicability and surface cited sections/definitions
- **Multi-Document Information Synthesis:** Find and connect information scattered across multiple documents to identify themes and correlate data points
- **Temporal and Conditional Logic Analysis:** Track time-series trends, implement conditional decision rules, and determine threshold-based alerts or actions
- **Research and Classification:** Analyze patterns, classify and identify relevant documents to recall specific information

## Scoring Approach

We use an LLM-based equality checker to evaluate responses:

\`\`\`
Assess whether the following CANDIDATE ANSWER is CORRECT or INCORRECT.
For the CANDIDATE ANSWER to be correct, it must be consistent with the OFFICIAL ANSWER.

The question, for reference only: {question}
The OFFICIAL ANSWER: {official_answer}
CANDIDATE ANSWER TO ASSESS: {candidate_answer}

Reply only with CORRECT or INCORRECT.

\`\`\`

Qwen3 235B A22B 2507 Non-reasoning is used as the equality checker model.

## Access and Citation

The AA-LCR dataset is available at [https://huggingface.co/datasets/ArtificialAnalysis/AA-LCR](https://huggingface.co/datasets/ArtificialAnalysis/AA-LCR).

If you use AA-LCR in your research, please cite:

\`\`\`json
@dataset{artificialanalysis2025lcr,
  title={Artificial Analysis Long Context Reasoning Benchmark(LCR)},
  author={Artificial Analysis Team},
  year={2025},
  publisher={Artificial Analysis, Inc.}
}
\`\`\`