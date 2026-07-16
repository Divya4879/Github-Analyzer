import os
from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry._logs import set_logger_provider
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor

OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "gitintel")

resource = Resource.create({"service.name": SERVICE_NAME})


def setup_telemetry(app):
    # Traces
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=OTLP_ENDPOINT, insecure=True))
    )
    trace.set_tracer_provider(tracer_provider)

    # Metrics
    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=OTLP_ENDPOINT, insecure=True)
    )
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    # Logs
    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=OTLP_ENDPOINT, insecure=True))
    )
    set_logger_provider(logger_provider)

    # Auto-instrument
    FastAPIInstrumentor.instrument_app(app)
    RequestsInstrumentor().instrument()


tracer = trace.get_tracer(SERVICE_NAME)
meter = metrics.get_meter(SERVICE_NAME)

# Metrics instruments
github_ratelimit_gauge = meter.create_gauge(
    "github.ratelimit.remaining",
    description="GitHub API rate limit remaining",
)
gemini_tokens_counter = meter.create_counter(
    "gemini.tokens.total",
    description="Total Gemini tokens used",
    unit="tokens",
)
gemini_prompt_tokens_counter = meter.create_counter(
    "gemini.tokens.prompt",
    description="Gemini prompt tokens used",
    unit="tokens",
)
gemini_completion_tokens_counter = meter.create_counter(
    "gemini.tokens.completion",
    description="Gemini completion tokens used",
    unit="tokens",
)
files_processed_counter = meter.create_counter(
    "github.files.processed",
    description="Total files fetched and processed",
)
loc_processed_counter = meter.create_counter(
    "github.loc.processed",
    description="Total lines of code processed",
    unit="lines",
)
assessment_duration_histogram = meter.create_histogram(
    "assessment.duration",
    description="Time taken to assess a repo",
    unit="s",
)
api_error_counter = meter.create_counter(
    "api.errors",
    description="API errors by service",
)
