from sslyze.server_connectivity_tester import ServerConnectivityTester, ServerConnectivityError
from sslyze.plugins.openssl_cipher_suites_plugin import *
from sslyze.plugins.certificate_info_plugin import CertificateInfoScanCommand
from sslyze.plugins.http_headers_plugin import HttpHeadersScanCommand, HttpHeadersScanResult
from sslyze.synchronous_scanner import SynchronousScanner
from sslyze.plugins.robot_plugin import RobotScanResultEnum, RobotScanCommand
from sslyze.plugins.heartbleed_plugin import HeartbleedScanCommand
from sslyze.plugins.openssl_ccs_injection_plugin import OpenSslCcsInjectionScanCommand

import requests
import logging

log = logging.getLogger('tlsprofiler')


class TLSProfilerResult:
    def __init__(self, validation_errors, profile_errors, vulnerability_errors):
        self.validation_errors = validation_errors
        self.profile_errors = profile_errors
        self.vulnerability_errors = vulnerability_errors

        self.validated = len(self.validation_errors) == 0
        self.profile_matched = len(self.profile_errors) == 0
        self.vulnerable = len(self.vulnerability_errors) > 0

        self.all_ok = self.validated and self.profile_matched and not self.vulnerable

    def __str__(self):
        return f"Validation Errors: {self.validation_errors}\n\nProfile Errors: {self.profile_errors}\n\n" \
               f"Vulnerability Errors: {self.vulnerability_errors}\n\nValidated: {self.validated}\n\n" \
               f"Profile Matched: {self.profile_matched}\n\nVulnerable: {self.vulnerable}\n\nAll ok: {self.all_ok}"


class TLSProfiler:
    PROFILES_URL = 'https://statics.tls.security.mozilla.org/server-side-tls-conf-5.0.json'
    PROFILES = None

    SCAN_COMMANDS = {
        "SSLv2": Sslv20ScanCommand,
        "SSLv3": Sslv30ScanCommand,
        "TLSv1": Tlsv10ScanCommand,
        "TLSv1.1": Tlsv11ScanCommand,
        "TLSv1.2": Tlsv12ScanCommand,
        "TLSv1.3": Tlsv13ScanCommand,
    }

    def __init__(self, domain, target_profile_name):
        if TLSProfiler.PROFILES is None:
            TLSProfiler.PROFILES = requests.get(self.PROFILES_URL).json()
            log.info(
                f"Loaded version {TLSProfiler.PROFILES['version']} of the Mozilla TLS configuration recommendations.")

        self.target_profile = TLSProfiler.PROFILES['configurations'][target_profile_name]

        self.scanner = SynchronousScanner()
        try:
            server_tester = ServerConnectivityTester(
                hostname=domain,
            )
            log.info(f'Testing connectivity with {server_tester.hostname}:{server_tester.port}...')
            self.server_info = server_tester.perform()
            self.server_error = None
        except ServerConnectivityError as e:
            # Could not establish an SSL connection to the server
            log.warning(f'Could not connect to {e.server_info.hostname}: {e.error_message}')
            self.server_error = e.error_message
            self.server_info = None

    def run(self):
        if self.server_info is None:
            return

        certificate_validation_errors = self.check_certificate()
        hsts_errors = self.check_hsts_age()
        self.scan_supported_ciphers_and_protocols()
        profile_errors = self.check_server_matches_profile()
        vulnerability_errors = self.check_vulnerabilities()

        return TLSProfilerResult(
            certificate_validation_errors,
            profile_errors + hsts_errors,
            vulnerability_errors,
        )

    def scan(self, command):
        return self.scanner.run_scan_command(self.server_info, command)

    def scan_supported_ciphers_and_protocols(self):
        supported_ciphers = []
        supported_protocols = []
        for name, command in self.SCAN_COMMANDS.items():
            log.debug(f"Testing protocol {name}")
            result = self.scan(command())
            ciphers = [cipher.openssl_name for cipher in result.accepted_cipher_list]
            supported_ciphers.extend(ciphers)
            if len(ciphers):
                supported_protocols.append(name)

        self.supported_ciphers = set(supported_ciphers)
        self.supported_protocols = set(supported_protocols)

    def check_server_matches_profile(self):
        errors = []

        allowed_protocols = set(self.target_profile['tls_versions'])
        illegal_protocols = self.supported_protocols - allowed_protocols

        for protocol in illegal_protocols:
            errors.append(f'must not support "{protocol}"')

        allowed_ciphers = set(self.target_profile['openssl_ciphersuites'] + self.target_profile['openssl_ciphers'])
        illegal_ciphers = self.supported_ciphers - allowed_ciphers

        for cipher in illegal_ciphers:
            errors.append(f'must not support "{cipher}"')

        return errors

    def check_hsts_age(self) -> List[str]:
        result = self.scan(HttpHeadersScanCommand())  # type: HttpHeadersScanResult

        errors = []

        if result.strict_transport_security_header:
            if result.strict_transport_security_header.max_age < self.target_profile['hsts_min_age']:
                errors.append(f"wrong HSTS age {result.strict_transport_security_header.max_age}")
        else:
            errors.append(f"HSTS header not set")

        return errors

    def check_certificate(self):
        result = self.scan(CertificateInfoScanCommand())

        errors = []

        for r in result.path_validation_result_list:
            if not r.was_validation_successful:
                errors.append(f"validation not successful: {r.verify_string} (trust store {r.trust_store.name})")

        if result.path_validation_error_list:
            validation_errors = (fail.error_message for fail in result.path_validation_error_list)
            errors.append(f'Validation failed: {", ".join(validation_errors)}')

        if not result.leaf_certificate_subject_matches_hostname:
            errors.append(f'Leaf certificate subject does not match hostname!')

        if not result.received_chain_has_valid_order:
            errors.append(f'Certificate chain has wrong order.')

        if result.verified_chain_has_sha1_signature:
            errors.append(f'SHA1 signature found in chain.')

        if result.verified_chain_has_legacy_symantec_anchor:
            errors.append(f'Symantec legacy certificate found in chain.')

        if result.leaf_certificate_signed_certificate_timestamps_count < 2:
            errors.append(
                f'Not enought SCTs in certificate, only found {result.leaf_certificate_signed_certificate_timestamps_count}.')

        if len(errors) == 0:
            log.debug(f"Certificate is ok")
        else:
            log.debug(f"Error validating certificate")
            for error in errors:
                log.debug(f"  → {error}")

        return errors

    def check_vulnerabilities(self):
        errors = []

        result = self.scan(HeartbleedScanCommand())

        if result.is_vulnerable_to_heartbleed:
            errors.append(f'Server is vulnerable to Heartbleed attack')

        result = self.scan(OpenSslCcsInjectionScanCommand())

        if result.is_vulnerable_to_ccs_injection:
            errors.append(f'Server is vulnerable to OpenSSL CCS Injection (CVE-2014-0224)')

        result = self.scan(RobotScanCommand())

        if result.robot_result_enum in [
            RobotScanResultEnum.VULNERABLE_WEAK_ORACLE,
            RobotScanResultEnum.VULNERABLE_STRONG_ORACLE,
        ]:
            errors.append(f"Server is vulnerable to ROBOT attack.")

        return errors


if __name__ == "__main__":
    ca_file = "../../tlsprofiler_test/tests/certificates/rsa_ca_cert.pem"
    profiler = TLSProfiler('old.dev.intranet', 'old')
    print(profiler.run())
