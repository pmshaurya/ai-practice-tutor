# Design notes

## The problem

Learners finish courses but can't answer three questions: what can I actually do now, what should I practise next, and what evidence would convince an employer? Certificates show completion, not skill. This prototype tests one answer: a short, realistic work task with feedback, retries and an honest record of what the learner showed.

I treated learning as the core and the employer report as a by-product of it. The principle: **help the learner freely, and record honestly how much help they needed.**

## Defining "good" before building

I started from what an entry-level operations analyst needs to do and turned each into one rubric skill:

| Skill | What it checks |
|---|---|
| Metric calculation | Uses the right rate to compare periods fairly, not raw totals |
| Interpretation | Explains what the change in the rate means for performance |
| Recommendation | Proposes a next step the data supports |

Each skill has three levels (Not yet shown, Partially, Demonstrated) written as observable behaviour, so any grader, human or AI, scores the same answer the same way.

The **rubric** describes the skill and never changes. Each task has its own **answer key** describing what a good answer looks like on that data. Sample recommendations are examples, not the only valid answers. A recommendation counts only if it passes four tests: tied to the finding, consistent with the data, proportionate, and checkable this week.

## Where AI belongs, and where it doesn't

Learners write free text, and the same correct idea comes in many forms: "6%", "0.06", "1 in 16.7", "a 25% drop". A keyword grader rejects correct answers phrased differently and accepts weak ones that happen to use the right words. The mock mode in this repo is exactly that kind of grader, and it fails test T2 for that reason.

So the LLM judges meaning, and code enforces everything that must never vary:

| The LLM handles | Code handles |
|---|---|
| Judging free text against each rubric level | Attempt limit and hint limit |
| Answering the four recommendation tests | The recommendation level, decided from those tests |
| Quoting the learner's words as evidence | Downgrading any skill whose quote isn't in the learner's answer |
| Writing feedback, guiding questions and hints | Replacing any feedback that contains an answer value the learner hasn't written |
| Writing the next step and employer conclusion | Fixed replies when asked for the answer or abused, the score, and sharing only when the learner chooses |

If the LLM is unavailable, the attempt isn't counted and the learner is asked to try again. Uncertain judgements are flagged for human review.

## The learner experience

- **Feedback is honest and points at gaps.** It never gives the answer, and when a learner's next step falls short, it names their suggestion and says why.
- **3 attempts, one free hint per task.** Scores stay hidden during practice so the focus is on thinking, not points.
- **Lessons for unknown concepts.** If a learner says they don't know something, they're pointed to a short lesson.
- **Retakes use new data.** A retake gives a different task with the same rubric, so it can't be passed from memory.

## The employer view

The learner decides when to share. Employers get a score out of 10 for quick sorting, based on which attempt each skill was first shown: full points on the first attempt, one less for each extra attempt. Behind the score sit evidence quotes in the learner's own words, unscored practice notes (like a result with no working shown), the full attempt-by-attempt timeline, and a plain statement that one short task is limited evidence.

## What testing changed

I tested by playing different learners and fixed what broke:

- **A correct result without working scored unfairly low.** A learner who stated a correct 25% drop without writing both rates lost all calculation points, scoring lower than someone who needed a hint. Correct results that depend on both rates now count, and presentation gaps moved to unscored practice notes.
- **Feedback ignored the learner's next step.** It asked whether their next step came from the data, which read as if they hadn't given one. Feedback now quotes the suggestion and states which test it fails.
- **Guiding questions were too specific.** Early versions nearly revealed the method. They now point at each gap without hinting at the fix.

## What's next

- More task variants, ideally generated from templates with the answer key computed by code
- A multi-learner employer view with filtering
- A human review queue for flagged judgements
- A small pilot measuring whether learners show a previously missing skill on a new task
