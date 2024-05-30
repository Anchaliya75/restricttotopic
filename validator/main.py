import contextvars
import json
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from guardrails.utils.casting_utils import to_int
from guardrails.utils.openai_utils import OpenAIClient
from guardrails.validator_base import (
    FailResult,
    PassResult,
    ValidationResult,
    Validator,
    register_validator,
)
from tenacity import retry, stop_after_attempt, wait_random_exponential
from transformers import pipeline


@register_validator(name="tryolabs/restricttotopic", data_type="string")
class RestrictToTopic(Validator):
    """Checks if text's main topic is specified within a list of valid topics
    and ensures that the text is not about any of the invalid topics.

    This validator accepts at least one valid topic and an optional list of
    invalid topics.

    Default behavior first runs a Zero-Shot model, and then falls back to
    ask OpenAI's `gpt-3.5-turbo` if the Zero-Shot model is not confident
    in the topic classification (score < 0.5).

    In our experiments this LLM fallback increases accuracy by 15% but also
    increases latency (more than doubles the latency in the worst case).

    Both the Zero-Shot classification and the GPT classification may be toggled.

    **Key Properties**

    | Property                      | Description                              |
    | ----------------------------- | ---------------------------------------- |
    | Name for `format` attribute   | `tryolabs/restricttotopic`               |
    | Supported data types          | `string`                                 |
    | Programmatic fix              | Removes lines with off-topic information |

    Args:
        valid_topics (List[str]): topics that the text should be about
            (one or many).
        invalid_topics (List[str], Optional, defaults to []): topics that the
            text cannot be about.
        device (int, Optional, defaults to -1): Device ordinal for CPU/GPU
            supports for Zero-Shot classifier. Setting this to -1 will leverage
            CPU, a positive will run the Zero-Shot model on the associated CUDA
            device id.
        model (str, Optional, defaults to 'facebook/bart-large-mnli'): The
            Zero-Shot model that will be used to classify the topic. See a
            list of all models here:
            https://huggingface.co/models?pipeline_tag=zero-shot-classification
        llm_callable (Union[str, Callable, None], Optional, defaults to
            'gpt-3.5-turbo'): Either the name of the OpenAI model, or a callable
            that takes a prompt and returns a response.
        disable_classifier (bool, Optional, defaults to False): controls whether
            to use the Zero-Shot model. At least one of disable_classifier and
            disable_llm must be False.
        disable_llm (bool, Optional, defaults to False): controls whether to use
            the LLM fallback. At least one of disable_classifier and
            disable_llm must be False.
        model_threshold (float, Optional, defaults to 0.5): The threshold used to
            determine whether to accept a topic from the Zero-Shot model. Must be
            a number between 0 and 1.
    """

    def __init__(
        self,
        valid_topics: List[str],
        invalid_topics: Optional[List[str]] = [],
        device: Optional[int] = -1,
        model: Optional[str] = "facebook/bart-large-mnli",
        llm_callable: Union[str, Callable, None] = None,
        disable_classifier: Optional[bool] = False,
        disable_llm: Optional[bool] = False,
        on_fail: Optional[Callable[..., Any]] = None,
        model_threshold: Optional[float] = 0.5,
    ):
        super().__init__(
            valid_topics=valid_topics,
            invalid_topics=invalid_topics,
            device=device,
            model=model,
            disable_classifier=disable_classifier,
            disable_llm=disable_llm,
            llm_callable=llm_callable,
            on_fail=on_fail,
            model_threshold=model_threshold,
        )
        self._valid_topics = valid_topics

        if invalid_topics is None:
            self._invalid_topics = []
        else:
            self._invalid_topics = invalid_topics

        self._device = device if device == "mps" else to_int(device)
        self._model = model
        self._disable_classifier = disable_classifier
        self._disable_llm = disable_llm

        if not model_threshold:
            model_threshold = 0.5
        else:
            self._model_threshold = model_threshold

        self.set_callable(llm_callable)
        self.classifier = pipeline(
            "zero-shot-classification",
            model=self._model,
            device=self._device,
            hypothesis_template="This example has to do with topic {}.",
            multi_label=True,
        )

    def get_topic_ensemble(
        self, text: str, candidate_topics: List[str]
    ) -> ValidationResult:
        topics, scores = self.get_topic_zero_shot(text, candidate_topics)
        failed = []
        for score, topic in zip(scores, topics):
            if score > self._model_threshold and topic in self._invalid_topics:
                failed.append(topic)

        if failed:
            return FailResult(
                error_message=f"The following invalid topics were found to be relevant: {failed}",
            )
        return self.get_topic_llm(text, candidate_topics)

    def get_topic_llm(self, text: str, candidate_topics: List[str]) -> ValidationResult:
        response = self.call_llm(text, candidate_topics)
        topic = json.loads(response)["topic"]
        return self.verify_topic(topic)

    def get_client_args(self) -> Tuple[Optional[str], Optional[str]]:
        kwargs = {}
        context_copy = contextvars.copy_context()
        for key, context_var in context_copy.items():
            if key.name == "kwargs" and isinstance(kwargs, dict):
                kwargs = context_var
                break

        api_key = kwargs.get("api_key")
        api_base = kwargs.get("api_base")

        return (api_key, api_base)

    @retry(
        wait=wait_random_exponential(min=1, max=60),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def call_llm(self, text: str, topics: List[str]) -> str:
        """Call the LLM with the given prompt.

        Expects a function that takes a string and returns a string.
        Args:
            text (str): The input text to classify using the LLM.
            topics (List[str]): The list of candidate topics.
        Returns:
            response (str): String representing the LLM response.
        """
        return self._llm_callable(text, topics)

    def verify_topic(self, topic: str) -> ValidationResult:
        if topic in self._valid_topics:
            return PassResult()
        else:
            return FailResult(error_message=f"Most relevant topic is {topic}.")

    def set_callable(self, llm_callable: Union[str, Callable, None]) -> None:
        """Set the LLM callable.

        Args:
            llm_callable: Either the name of the OpenAI model, or a callable that takes
                a prompt and returns a response.
        """

        if llm_callable is None:
            llm_callable = "gpt-3.5-turbo"

        if isinstance(llm_callable, str):
            if llm_callable not in ["gpt-3.5-turbo", "gpt-4"]:
                raise ValueError(
                    "llm_callable must be one of 'gpt-3.5-turbo' or 'gpt-4'."
                    "If you want to use a custom LLM, please provide a callable."
                    "Check out ProvenanceV1 documentation for an example."
                )

            def openai_callable(text: str, topics: List[str]) -> str:
                api_key, api_base = self.get_client_args()
                response = OpenAIClient(api_key, api_base).create_chat_completion(
                    model=llm_callable,
                    response_format={"type": "json_object"},
                    messages=[
                        {
                            "role": "user",
                            "content": f"""Classify the following text {text}
                                into one of these topics: {topics}.
                                Format the response as JSON with the following schema:
                                {{"topic": "topic_name"}}""",
                        },
                    ],
                )

                return response.output

            self._llm_callable = openai_callable
        elif isinstance(llm_callable, Callable):
            self._llm_callable = llm_callable
        else:
            raise ValueError("llm_callable must be a string or a Callable")

    def get_topic_zero_shot(
        self, text: str, candidate_topics: List[str]
    ) -> Tuple[str, float]:
        result = self.classifier(text, candidate_topics)
        topics = result["labels"]
        scores = result["scores"]
        return topics, scores

    def validate(
        self, value: str, metadata: Optional[Dict[str, Any]] = {}
    ) -> ValidationResult:
        valid_topics = set(self._valid_topics)
        invalid_topics = set(self._invalid_topics)

        # throw if valid and invalid topics are empty
        if not valid_topics:
            raise ValueError(
                "`valid_topics` must be set and contain at least one topic."
            )
        if not invalid_topics:
            raise ValueError(
                "`invalid topics` must be set and contain at least one topic."
            )
        # throw if valid and invalid topics are not disjoint
        if bool(valid_topics.intersection(invalid_topics)):
            raise ValueError("A topic cannot be valid and invalid at the same time.")

        # Check which model(s) to use
        if self._disable_classifier and self._disable_llm:  # Error, no model set
            raise ValueError("Either classifier or llm must be enabled.")
        elif (
            not self._disable_classifier and not self._disable_llm
        ):  # Use ensemble (Zero-Shot + Ensemble)
            return self.get_topic_ensemble(value, list(invalid_topics))
        elif self._disable_classifier and not self._disable_llm:  # Use only LLM
            return self.get_topic_llm(value, list(invalid_topics))

        # Use only Zero-Shot
        topics, scores = self.get_topic_zero_shot(
            value, list(invalid_topics) + list(valid_topics)
        )
        succesfully_on_topic = []
        for score, topic in zip(scores, topics):
            if score > self._model_threshold and topic in self._valid_topics:
                succesfully_on_topic.append(topic)
            if score > self._model_threshold and topic in self._invalid_topics:
                return FailResult(
                    error_message=f"Invalid {topic} was found to be relevant."
                )
        if not succesfully_on_topic:
            return FailResult(error_message="No valid topic was found.")
        return PassResult()
