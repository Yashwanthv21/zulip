import mock
from mock import call
import time
from typing import Any, Dict, Union, SupportsInt, Text

import gcm

from django.test import TestCase
from django.conf import settings

from zerver.models import PushDeviceToken, UserProfile, Message
from zerver.models import get_user_profile_by_email, receives_online_notifications, \
    receives_offline_notifications
from zerver.lib import push_notifications as apn
from zerver.lib.test_classes import (
    ZulipTestCase,
)

class MockRedis(object):
    data = {}  # type: Dict[str, Any]

    def hgetall(self, key):
        # type: (str) -> Any
        return self.data.get(key)

    def exists(self, key):
        # type: (str) -> bool
        return key in self.data

    def hmset(self, key, data):
        # type: (str, Dict[Any, Any]) -> None
        self.data[key] = data

    def delete(self, key):
        # type: (str) -> None
        if self.exists(key):
            del self.data[key]

    def expire(self, *args, **kwargs):
        # type: (*Any, **Any) -> None
        pass

class PushNotificationTest(TestCase):
    def setUp(self):
        # type: () -> None
        email = 'hamlet@zulip.com'
        apn.connection = apn.get_connection('fake-cert', 'fake-key')
        self.redis_client = apn.redis_client = MockRedis()  # type: ignore
        apn.dbx_connection = apn.get_connection('fake-cert', 'fake-key')
        self.user_profile = get_user_profile_by_email(email)
        self.tokens = [u'aaaa', u'bbbb']
        for token in self.tokens:
            PushDeviceToken.objects.create(
                kind=PushDeviceToken.APNS,
                token=apn.hex_to_b64(token),
                user=self.user_profile,
                ios_app_id=settings.ZULIP_IOS_APP_ID)

    def tearDown(self):
        # type: () -> None
        for i in [100, 200]:
            self.redis_client.delete(apn.get_apns_key(i))

class APNsMessageTest(PushNotificationTest):
    @mock.patch('random.getrandbits', side_effect=[100, 200])
    def test_apns_message(self, mock_getrandbits):
        # type: (mock.MagicMock) -> None
        apn.APNsMessage(self.user_profile, self.tokens, alert="test")
        data = self.redis_client.hgetall(apn.get_apns_key(100))
        self.assertEqual(data['token'], 'aaaa')
        self.assertEqual(int(data['user_id']), self.user_profile.id)
        data = self.redis_client.hgetall(apn.get_apns_key(200))
        self.assertEqual(data['token'], 'bbbb')
        self.assertEqual(int(data['user_id']), self.user_profile.id)

class ResponseListenerTest(PushNotificationTest):
    def get_error_response(self, **kwargs):
        # type: (**Any) -> Dict[str, SupportsInt]
        er = {'identifier': 0, 'status': 0}  # type: Dict[str, SupportsInt]
        er.update({k: v for k, v in kwargs.items() if k in er})
        return er

    def get_cache_value(self):
        # type: () -> Dict[str, Union[str, int]]
        return {'token': 'aaaa', 'user_id': self.user_profile.id}

    @mock.patch('logging.warn')
    def test_cache_does_not_exist(self, mock_warn):
        # type: (mock.MagicMock) -> None
        err_rsp = self.get_error_response(identifier=100, status=1)
        apn.response_listener(err_rsp)
        msg = "APNs key, apns:100, doesn't not exist."
        mock_warn.assert_called_once_with(msg)

    @mock.patch('logging.warn')
    def test_cache_exists(self, mock_warn):
        # type: (mock.MagicMock) -> None
        self.redis_client.hmset(apn.get_apns_key(100), self.get_cache_value())
        err_rsp = self.get_error_response(identifier=100, status=1)
        apn.response_listener(err_rsp)
        b64_token = apn.hex_to_b64('aaaa')
        errmsg = apn.ERROR_CODES[int(err_rsp['status'])]
        msg = ("APNS: Failed to deliver APNS notification to %s, "
               "reason: %s" % (b64_token, errmsg))
        mock_warn.assert_called_once_with(msg)

    @mock.patch('logging.warn')
    def test_error_code_eight(self, mock_warn):
        # type: (mock.MagicMock) -> None
        self.redis_client.hmset(apn.get_apns_key(100), self.get_cache_value())
        err_rsp = self.get_error_response(identifier=100, status=8)
        b64_token = apn.hex_to_b64('aaaa')
        self.assertEqual(PushDeviceToken.objects.filter(
            user=self.user_profile, token=b64_token).count(), 1)
        apn.response_listener(err_rsp)
        self.assertEqual(mock_warn.call_count, 2)
        self.assertEqual(PushDeviceToken.objects.filter(
            user=self.user_profile, token=b64_token).count(), 0)

class TestPushApi(ZulipTestCase):
    def test_push_api(self):
        # type: () -> None
        email = "cordelia@zulip.com"
        user = get_user_profile_by_email(email)
        self.login(email)

        endpoints = [
            ('/json/users/me/apns_device_token', 'apple-token'),
            ('/json/users/me/android_gcm_reg_id', 'android-token'),
        ]

        # Test error handling
        for endpoint, _ in endpoints:
            # Try adding/removing tokens that are too big...
            broken_token = "x" * 5000 # too big
            result = self.client_post(endpoint, {'token': broken_token})
            self.assert_json_error(result, 'Empty or invalid length token')

            result = self.client_delete(endpoint, {'token': broken_token})
            self.assert_json_error(result, 'Empty or invalid length token')

            # Try to remove a non-existent token...
            result = self.client_delete(endpoint, {'token': 'non-existent token'})
            self.assert_json_error(result, 'Token does not exist')

        # Add tokens
        for endpoint, token in endpoints:
            # Test that we can push twice
            result = self.client_post(endpoint, {'token': token})
            self.assert_json_success(result)

            result = self.client_post(endpoint, {'token': token})
            self.assert_json_success(result)

            tokens = list(PushDeviceToken.objects.filter(user=user, token=token))
            self.assertEqual(len(tokens), 1)
            self.assertEqual(tokens[0].token, token)

        # User should have tokens for both devices now.
        tokens = list(PushDeviceToken.objects.filter(user=user))
        self.assertEqual(len(tokens), 2)

        # Remove tokens
        for endpoint, token in endpoints:
            result = self.client_delete(endpoint, {'token': token})
            self.assert_json_success(result)
            tokens = list(PushDeviceToken.objects.filter(user=user, token=token))
            self.assertEqual(len(tokens), 0)

class SendNotificationTest(PushNotificationTest):
    @mock.patch('logging.warn')
    @mock.patch('logging.info')
    @mock.patch('zerver.lib.push_notifications._do_push_to_apns_service')
    def test_send_apple_push_notifiction(self, mock_send, mock_info, mock_warn):
        # type: (mock.MagicMock, mock.MagicMock, mock.MagicMock) -> None
        def test_send(user, message, alert):
            # type: (UserProfile, Message, str) -> None
            self.assertEqual(user.id, self.user_profile.id)
            self.assertEqual(set(message.tokens), set(self.tokens))

        mock_send.side_effect = test_send
        apn.send_apple_push_notification(self.user_profile, "test alert")
        self.assertEqual(mock_send.call_count, 1)

    @mock.patch('apns.GatewayConnection.send_notification_multiple')
    def test_do_push_to_apns_service(self, mock_push):
        # type: (mock.MagicMock) -> None
        msg = apn.APNsMessage(self.user_profile, self.tokens, alert="test")

        def test_push(message):
            # type: (Message) -> None
            self.assertIs(message, msg.get_frame())

        mock_push.side_effect = test_push
        apn._do_push_to_apns_service(self.user_profile, msg, apn.connection)

    @mock.patch('logging.warn')
    @mock.patch('logging.info')
    @mock.patch('apns.GatewayConnection.send_notification_multiple')
    def test_connection_single_none(self, mock_push, mock_info, mock_warn):
        # type: (mock.MagicMock, mock.MagicMock, mock.MagicMock) -> None
        apn.connection = None
        apn.send_apple_push_notification(self.user_profile, "test alert")

    @mock.patch('logging.error')
    @mock.patch('apns.GatewayConnection.send_notification_multiple')
    def test_connection_both_none(self, mock_push, mock_error):
        # type: (mock.MagicMock, mock.MagicMock) -> None
        apn.connection = None
        apn.dbx_connection = None
        apn.send_apple_push_notification(self.user_profile, "test alert")

class APNsFeedbackTest(PushNotificationTest):
    @mock.patch('logging.info')
    @mock.patch('apns.FeedbackConnection.items')
    def test_feedback(self, mock_items, mock_info):
        # type: (mock.MagicMock, mock.MagicMock) -> None
        update_time = apn.timestamp_to_datetime(int(time.time()) - 10000)
        PushDeviceToken.objects.all().update(last_updated=update_time)
        mock_items.return_value = [
            ('aaaa', int(time.time())),
        ]
        self.assertEqual(PushDeviceToken.objects.all().count(), 2)
        apn.check_apns_feedback()
        self.assertEqual(PushDeviceToken.objects.all().count(), 1)

class GCMTest(PushNotificationTest):
    def setUp(self):
        # type: () -> None
        super(GCMTest, self).setUp()
        apn.gcm = gcm.GCM('fake key')
        self.gcm_tokens = [u'1111', u'2222']
        for token in self.gcm_tokens:
            PushDeviceToken.objects.create(
                kind=PushDeviceToken.GCM,
                token=apn.hex_to_b64(token),
                user=self.user_profile,
                ios_app_id=None)

    def get_gcm_data(self, **kwargs):
        # type: (**Any) -> Dict[str, Any]
        data = {
            'key 1': 'Data 1',
            'key 2': 'Data 2',
        }
        data.update(kwargs)
        return data

class GCMNotSetTest(GCMTest):
    @mock.patch('logging.error')
    def test_gcm_is_none(self, mock_error):
        # type: (mock.MagicMock) -> None
        apn.gcm = None
        apn.send_android_push_notification(self.user_profile, {})
        mock_error.assert_called_with("Attempting to send a GCM push "
                                      "notification, but no API key was "
                                      "configured")

class GCMSuccessTest(GCMTest):
    @mock.patch('logging.warning')
    @mock.patch('logging.info')
    @mock.patch('gcm.GCM.json_request')
    def test_success(self, mock_send, mock_info, mock_warning):
        # type: (mock.MagicMock, mock.MagicMock, mock.MagicMock) -> None
        res = {}
        res['success'] = {token: ind for ind, token in enumerate(self.gcm_tokens)}
        mock_send.return_value = res

        data = self.get_gcm_data()
        apn.send_android_push_notification(self.user_profile, data)
        self.assertEqual(mock_info.call_count, 2)
        c1 = call("GCM: Sent 1111 as 0")
        c2 = call("GCM: Sent 2222 as 1")
        mock_info.assert_has_calls([c1, c2], any_order=True)
        mock_warning.assert_not_called()

class GCMCanonicalTest(GCMTest):
    @mock.patch('logging.warning')
    @mock.patch('gcm.GCM.json_request')
    def test_equal(self, mock_send, mock_warning):
        # type: (mock.MagicMock, mock.MagicMock) -> None
        res = {}
        res['canonical'] = {1: 1}
        mock_send.return_value = res

        data = self.get_gcm_data()
        apn.send_android_push_notification(self.user_profile, data)
        mock_warning.assert_called_once_with("GCM: Got canonical ref but it "
                                             "already matches our ID 1!")

    @mock.patch('logging.warning')
    @mock.patch('gcm.GCM.json_request')
    def test_pushdevice_not_present(self, mock_send, mock_warning):
        # type: (mock.MagicMock, mock.MagicMock) -> None
        res = {}
        t1 = apn.hex_to_b64(u'1111')
        t2 = apn.hex_to_b64(u'3333')
        res['canonical'] = {t1: t2}
        mock_send.return_value = res

        def get_count(hex_token):
            # type: (Text) -> int
            token = apn.hex_to_b64(hex_token)
            return PushDeviceToken.objects.filter(
                token=token, kind=PushDeviceToken.GCM).count()

        self.assertEqual(get_count(u'1111'), 1)
        self.assertEqual(get_count(u'3333'), 0)

        data = self.get_gcm_data()
        apn.send_android_push_notification(self.user_profile, data)
        msg = ("GCM: Got canonical ref %s "
               "replacing %s but new ID not "
               "registered! Updating.")
        mock_warning.assert_called_once_with(msg % (t2, t1))

        self.assertEqual(get_count(u'1111'), 0)
        self.assertEqual(get_count(u'3333'), 1)

    @mock.patch('logging.info')
    @mock.patch('gcm.GCM.json_request')
    def test_pushdevice_different(self, mock_send, mock_info):
        # type: (mock.MagicMock, mock.MagicMock) -> None
        res = {}
        old_token = apn.hex_to_b64(u'1111')
        new_token = apn.hex_to_b64(u'2222')
        res['canonical'] = {old_token: new_token}
        mock_send.return_value = res

        def get_count(hex_token):
            # type: (Text) -> int
            token = apn.hex_to_b64(hex_token)
            return PushDeviceToken.objects.filter(
                token=token, kind=PushDeviceToken.GCM).count()

        self.assertEqual(get_count(u'1111'), 1)
        self.assertEqual(get_count(u'2222'), 1)

        data = self.get_gcm_data()
        apn.send_android_push_notification(self.user_profile, data)
        mock_info.assert_called_once_with(
            "GCM: Got canonical ref %s, dropping %s" % (new_token, old_token))

        self.assertEqual(get_count(u'1111'), 0)
        self.assertEqual(get_count(u'2222'), 1)

class GCMNotRegisteredTest(GCMTest):
    @mock.patch('logging.info')
    @mock.patch('gcm.GCM.json_request')
    def test_not_registered(self, mock_send, mock_info):
        # type: (mock.MagicMock, mock.MagicMock) -> None
        res = {}
        token = apn.hex_to_b64(u'1111')
        res['errors'] = {'NotRegistered': [token]}
        mock_send.return_value = res

        def get_count(hex_token):
            # type: (Text) -> int
            token = apn.hex_to_b64(hex_token)
            return PushDeviceToken.objects.filter(
                token=token, kind=PushDeviceToken.GCM).count()

        self.assertEqual(get_count(u'1111'), 1)

        data = self.get_gcm_data()
        apn.send_android_push_notification(self.user_profile, data)
        mock_info.assert_called_once_with("GCM: Removing %s" % (token,))
        self.assertEqual(get_count(u'1111'), 0)

class GCMFailureTest(GCMTest):
    @mock.patch('logging.warning')
    @mock.patch('gcm.GCM.json_request')
    def test_failure(self, mock_send, mock_warn):
        # type: (mock.MagicMock, mock.MagicMock) -> None
        res = {}
        token = apn.hex_to_b64(u'1111')
        res['errors'] = {'Failed': [token]}
        mock_send.return_value = res

        data = self.get_gcm_data()
        apn.send_android_push_notification(self.user_profile, data)
        c1 = call("GCM: Delivery to %s failed: Failed" % (token,))
        mock_warn.assert_has_calls([c1], any_order=True)

class TestReceivesNotificationsFunctions(ZulipTestCase):
    def setUp(self):
        # type: () -> None
        email = "cordelia@zulip.com"
        self.user = get_user_profile_by_email(email)

    def test_receivers_online_notifications_when_user_is_a_bot(self):
        # type: () -> None
        self.user.is_bot = True

        self.user.enable_online_push_notifications = True
        self.assertFalse(receives_online_notifications(self.user))

        self.user.enable_online_push_notifications = False
        self.assertFalse(receives_online_notifications(self.user))

    def test_receivers_online_notifications_when_user_is_not_a_bot(self):
        # type: () -> None
        self.user.is_bot = False

        self.user.enable_online_push_notifications = True
        self.assertTrue(receives_online_notifications(self.user))

        self.user.enable_online_push_notifications = False
        self.assertFalse(receives_online_notifications(self.user))

    def test_receivers_offline_notifications_when_user_is_a_bot(self):
        # type: () -> None
        self.user.is_bot = True

        self.user.enable_offline_email_notifications = True
        self.user.enable_offline_push_notifications = True
        self.assertFalse(receives_offline_notifications(self.user))

        self.user.enable_offline_email_notifications = False
        self.user.enable_offline_push_notifications = False
        self.assertFalse(receives_offline_notifications(self.user))

        self.user.enable_offline_email_notifications = True
        self.user.enable_offline_push_notifications = False
        self.assertFalse(receives_offline_notifications(self.user))

        self.user.enable_offline_email_notifications = False
        self.user.enable_offline_push_notifications = True
        self.assertFalse(receives_offline_notifications(self.user))

    def test_receivers_offline_notifications_when_user_is_not_a_bot(self):
        # type: () -> None
        self.user.is_bot = False

        self.user.enable_offline_email_notifications = True
        self.user.enable_offline_push_notifications = True
        self.assertTrue(receives_offline_notifications(self.user))

        self.user.enable_offline_email_notifications = False
        self.user.enable_offline_push_notifications = False
        self.assertFalse(receives_offline_notifications(self.user))

        self.user.enable_offline_email_notifications = True
        self.user.enable_offline_push_notifications = False
        self.assertTrue(receives_offline_notifications(self.user))

        self.user.enable_offline_email_notifications = False
        self.user.enable_offline_push_notifications = True
        self.assertTrue(receives_offline_notifications(self.user))
