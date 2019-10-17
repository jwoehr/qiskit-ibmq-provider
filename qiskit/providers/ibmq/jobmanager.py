# -*- coding: utf-8 -*-

# This code is part of Qiskit.
#
# (C) Copyright IBM 2019.
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Job manager used to manage jobs for IBM Q Experience."""

import logging
import time
from typing import List, Optional, Union
from collections import Counter
from concurrent import futures
import threading

from qiskit.circuit import QuantumCircuit
from qiskit.pulse import ScheduleComponent, Schedule
from qiskit.compiler import assemble, transpile
from qiskit.providers.jobstatus import JobStatus
from qiskit.providers.exceptions import JobError
from qiskit.result import Result
from qiskit.qobj import Qobj

from .ibmqbackend import IBMQBackend
from .utils.jobmanager_utils import requires_submit, submit_async
from .job import IBMQJob
from .exceptions import IBMQJobManagerInvalidStateError, IBMQJobManagerTimeoutError

logger = logging.getLogger(__name__)


class JobManager:
    """Job manager for IBM Q Experience."""

    def __init__(self) -> None:
        """Creates a new JobManager instance."""
        self._jobs = []  # type: List[IBMQJob]

        # Used for async job submit
        self._executor = futures.ThreadPoolExecutor(1)
        self._submit_exception = None  # type: Optional[Exception]
        self._submit_done_event = threading.Event()
        self._submit_called = False

        # Used for caching
        self._result = None  # type: Optional[List[Union[Result, None]]]
        self._error_msg = None  # type: Optional[str]

    def run(
            self,
            experiments: Union[List[QuantumCircuit], List[Schedule]],
            backend: IBMQBackend,
            name_prefix: Optional[str] = None,
            shots: int = 1024,
            skip_transpile: bool = False,
            max_experiments_per_job: Optional[int] = None
    ) -> int:
        """Execute a list of circuits or pulse schedules on a backend.

        The circuits or schedules will be split into multiple jobs. Circuits
        or schedules in a job will be executed together in each shot.

        The circuits will first be transpiled before submitted, using the
        default transpiler settings. Specify ``skip_transpiler=True`` if you
        want to skip this step.

        Args:
            experiments : Circuit(s) or pulse schedule(s) to execute.
            backend: Backend to execute the experiments on.
            name_prefix: Prefix of the job name. Each job name will be the
                prefix followed by an underscore (_) followed by the job index.
            shots: Number of repetitions of each experiment, for sampling. Default: 1024.
            skip_transpile: True if transpilation is to be skipped, otherwise False.
            max_experiments_per_job: Maximum number of experiments to run in each job.
                If not specified, the default is to use the maximum allowed by
                the backend.
                If the specified value is greater the maximum allowed by the
                backend, the default is used.

        Returns:
            Number of jobs to be run.

        Raises:
            IBMQJobManagerInvalidStateError: If the backend does not support
                the experiment type, or if this method was previously called
                to run experiments.
        """
        if (any(isinstance(exp, ScheduleComponent) for exp in experiments) and
                not backend.configuration().open_pulse):
            raise IBMQJobManagerInvalidStateError("The backend does not support pulse schedules.")

        if self._submit_called:
            raise IBMQJobManagerInvalidStateError("Jobs were already submitted.")

        if not skip_transpile:
            experiments = transpile(circuits=experiments, backend=backend)
        experiment_list = self._split_experiments(experiments, backend=backend,
                                                  max_experiments_per_job=max_experiments_per_job)

        qobjs = []
        for experiment in experiment_list:
            qobjs.append(assemble(experiment, backend=backend, shots=shots))

        future = self._executor.submit(submit_async, qobjs=qobjs, backend=backend,
                                       name_prefix=name_prefix)
        future.add_done_callback(self._submit_callback)
        self._submit_called = True

        return len(qobjs)

    def _submit_callback(self, future: futures.Future) -> None:
        """Callback for when job submit is done.

        Args:
            future: Future object encapsulates the asynchronous execution of
                job submit.
        """
        results = future.result()
        for result in results:
            if isinstance(result, IBMQJob):
                self._jobs.append(result)
            else:
                self._submit_exception = result

        # Cleanup if submit failed
        if self._submit_exception:
            for job in self._jobs:
                job.cancel()
            self._jobs = []

        self._submit_done_event.set()

    @requires_submit
    def jobs(self) -> List[IBMQJob]:
        """Return a list of submitted jobs.

        Returns:
            A list of IBMQJob instances that represents the submitted jobs.
        """
        return self._jobs

    @requires_submit
    def status(self) -> List[Union[JobStatus, None]]:
        """Return the status of each job.

        Returns:
            A list of job statuses. The entry is ``None`` if the job status
                cannot be retrieved due to server error.
        """
        statuses = []
        for i, job in enumerate(self._jobs):
            try:
                statuses.append(job.status())
            except JobError as err:
                logger.warning("Unable to retrieve status for job %d (job ID=%s): %s",
                               i, job.job_id(), str(err))
                statuses.append(None)

        return statuses

    @requires_submit
    def report(self, detailed: bool = True) -> str:
        """Return a report on current job statuses.

        Args:
            detailed: True if a detailed report is be returned. False
                if a summary report is to be returned.

        Returns:
            A report on job statuses.
        """
        statuses = self.status()
        counts = Counter(statuses)

        report = [
            "Summary report:",
            "       Total jobs: {}".format(len(statuses)),
            "  Successful jobs: {}".format(counts[JobStatus.DONE]),
            "      Failed jobs: {}".format(counts[JobStatus.ERROR]),
            "   Cancelled jobs: {}".format(counts[JobStatus.CANCELLED]),
            "     Running jobs: {}".format(counts[JobStatus.RUNNING])
        ]

        if detailed:
            report.append("\nDetail report:")
            for i, status in enumerate(statuses):
                job = self._jobs[i]
                status_txt = status.value if status else "Unknown"
                report.append("  - Job {} -".format(i))
                report.append("    job ID: {}".format(job.job_id()))
                report.append("    name: {}".format(job.name()))
                report.append("    status: {}".format(status_txt))
                report.append("    experiments: {}-{}".format(job.start_index, job.end_index))
                if status is JobStatus.QUEUED:
                    report.append("    queue position: {}".format(job.queue_position()))
                elif status is JobStatus.ERROR:
                    report.append("    error_messsage:")
                    msg_list = job.error_message().split('\n')
                    for msg in msg_list:
                        report.append(msg.rjust(len(msg)+6))

        return '\n'.join(report)

    @requires_submit
    def result(self, timeout: Optional[float] = None) -> List[Union[Result, None]]:
        """Return the results of the jobs.

        This call will block until all job results become available or
            the timeout is reached.

        Args:
           timeout: Number of seconds to wait for job results.

        Returns:
            A list of job results. The entry is ``None`` if the job result
                cannot be retrieved.

        Raises:
            IBMQJobManagerTimeoutError: if unable to retrieve all job results before the
                specified timeout.
        """
        if self._result:
            return self._result

        results = []
        start_time = time.time()
        original_timeout = timeout

        for i, job in enumerate(self._jobs):
            result = None
            try:
                # TODO Revise this when partial result is supported
                result = job.result(timeout=timeout)
            except JobError as err:
                logger.warning("Unable to retrieve results for experiments "
                               "%d-%d (job %d, ID=%s): %s", job.start_index,
                               job.end_index, i, job.job_id(), str(err))

            results.append(result)
            if timeout:
                timeout = original_timeout - (time.time() - start_time)
                if timeout <= 0:
                    raise IBMQJobManagerTimeoutError(
                        "Timeout waiting for results for experiments "
                        "{}-{} (job {}, ID={}).".format(
                            job.start_index, job.end_index, i, job.job_id()))

        self._result = results
        return self._result

    @requires_submit
    def error_message(self) -> Optional[str]:
        """Provide details about job failures.

        Returns:
            An error report if one or more jobs failed or ``None`` otherwise.
        """
        if self._error_msg:
            return self._error_msg

        report = []
        for i, job in enumerate(self._jobs):
            if job.status() is not JobStatus.ERROR:
                continue
            report.append("Job {} (job ID={}) for experiments {}-{}:".format(
                i, job.job_id(), job.start_index, job.end_index))
            try:
                msg_list = job.error_message().split('\n')
            except JobError:
                msg_list = ["Unknown error."]

            for msg in msg_list:
                report.append(msg.rjust(len(msg)+2))

        if not report:
            return None
        return '\n'.join(report)

    @requires_submit
    def qobj(self) -> List[Qobj]:
        """Return the Qobj for the jobs.

        Returns:
            A list of Qobj for the jobs.
        """
        return [job.qobj() for job in self._jobs]

    def _split_experiments(
            self,
            experiments: Union[List[QuantumCircuit], List[Schedule]],
            backend: IBMQBackend,
            max_experiments_per_job: Optional[int] = None
    ) -> List[Union[List[QuantumCircuit], List[Schedule]]]:
        """Split a list of experiments into sublists.

        Args:
            experiments: Experiments to be split.
            backend: Backend to execute the experiments on.
            max_experiments_per_job: Maximum number of experiments to run in each job.

        Returns:
            A list of sublists of experiments.
        """
        if hasattr(backend.configuration(), 'max_experiments'):
            backend_max = backend.configuration().max_experiments
            chunk_size = backend_max if max_experiments_per_job is None \
                else min(backend_max, max_experiments_per_job)
        elif max_experiments_per_job:
            chunk_size = max_experiments_per_job
        else:
            return [experiments]

        return [experiments[x:x + chunk_size] for x in range(0, len(experiments), chunk_size)]
