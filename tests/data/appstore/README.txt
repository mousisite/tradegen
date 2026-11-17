Apple's own test data, used by tests/test_appstore.py.

Copied unchanged from Apple's App Store Server Library for Python
(https://github.com/apple/app-store-server-library-python), under the MIT
licence in APPLE_LICENSE.txt:

  mock_signed_data/*        tests/resources/mock_signed_data, signed by Apple
                            under its test root
  testCA.der                tests/resources/certs/testCA.der, that test root
  apple_x509_vectors.json   the certificate strings and EFFECTIVE_DATE from
                            tests/test_x509_verification.py, read out as data

AppleRootCA-G3.cer is Apple Root CA - G3 as Apple publishes it at
https://www.apple.com/certificateauthority/AppleRootCA-G3.cer, kept here so
the test can prove the copy built into bot/appstore.py is byte for byte the
same.

Nothing here is secret or personal.
