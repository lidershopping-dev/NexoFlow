import unittest
from copy import deepcopy

from contact_dedup_guard import contact_refusals, contact_uuid, validate_pair

OWNER_UUID = "11111111-1111-4111-8111-111111111111"
DUPLICATE_UUID = "22222222-2222-4222-8222-222222222222"
OTHER_UUID = "33333333-3333-4333-8333-333333333333"


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.owner = {
            "uuid": OWNER_UUID,
            "name": "Pessoa correta",
            "urns": ["whatsapp:+5591999998888"],
            "fields": {"cpf": "52998224725", "pedido_id": "123"},
        }
        self.duplicate = {
            "uuid": DUPLICATE_UUID,
            "name": "Outro perfil",
            "urns": ["whatsapp:+5591988887777"],
            "fields": {
                "cpf": "52998224725",
                "pedido_id": "123",
                "telefone": "+5591999998888",
            },
        }
        self.customer = {"id": 99, "cpf": "52998224725", "celular": "(91) 99999-8888"}
        self.orders = {"123": {"capa": {"id": 123, "codCliente": 99}}}

    def check(self, **kw):
        args = dict(
            customer=self.customer,
            related_contacts=[self.owner, self.duplicate],
            orders=self.orders,
        )
        args.update(kw)
        return validate_pair(self.owner, self.duplicate, **args)

    def test_unique_native_phone_and_both_official_endpoints_allow_pair(self):
        self.assertEqual(self.check(), [])

    def test_display_names_never_override_official_phone(self):
        self.owner["name"] = "🙂"
        self.duplicate["name"] = "Pessoa correta"
        self.assertEqual(self.check(), [])

    def test_different_customer_is_not_deleted(self):
        self.duplicate["fields"]["cpf"] = "16899535009"
        self.assertIn("CPF_SEM_VINCULO_VALIDO_UNICO", self.check())

    def test_zero_cpf_cannot_authorize_mass_deletion(self):
        for c in (self.owner, self.duplicate):
            c["fields"]["cpf"] = "0"
        self.customer["cpf"] = "0"
        self.assertIn("CPF_SEM_VINCULO_VALIDO_UNICO", self.check())

    def test_unknown_or_conflicting_order_blocks(self):
        self.assertIn("PEDIDO_NAO_COMPROVADO_DO_MESMO_CLIENTE", self.check(orders={}))
        self.orders["123"]["capa"]["codCliente"] = 999
        self.assertIn("PEDIDO_NAO_COMPROVADO_DO_MESMO_CLIENTE", self.check())

    def test_another_historical_customer_blocks(self):
        self.assertIn(
            "HISTORICO_DE_OUTRO_CLIENTE", self.check(history_owner_ids={"99", "456"})
        )

    def test_generic_custom_phone_does_not_establish_identity(self):
        self.owner["urns"] = ["instagram:synthetic"]
        self.owner["fields"]["telefone"] = self.customer["celular"]
        self.assertIn("TELEFONE_SEM_CONTATO_CANONICO_UNICO", self.check())

    def test_matching_local_number_in_another_area_is_not_enough_for_deletion(self):
        self.owner["urns"] = ["whatsapp:+5592999998888"]
        self.assertIn("TELEFONE_SEM_CONTATO_CANONICO_UNICO", self.check())

    def test_two_phone_matching_contacts_are_ambiguous(self):
        other = deepcopy(self.owner)
        other["uuid"] = OTHER_UUID
        self.assertIn(
            "TELEFONE_SEM_CONTATO_CANONICO_UNICO",
            self.check(related_contacts=[self.owner, self.duplicate, other]),
        )

    def test_no_order_duplicate_can_be_resolved_by_valid_cpf_and_unique_phone(self):
        self.duplicate["fields"].pop("pedido_id")
        self.assertEqual(self.check(), [])

    def test_recorded_order_not_in_current_weni_fields_must_also_be_verified(self):
        self.assertIn(
            "PEDIDO_NAO_COMPROVADO_DO_MESMO_CLIENTE",
            self.check(recorded_order_ids={"124"}),
        )

    def test_omitted_or_noncanonical_uuid_never_becomes_a_delete_selector(self):
        for bad in (
            None,
            "",
            "right",
            True,
            123,
            OWNER_UUID.replace("-", ""),
            "{" + OWNER_UUID + "}",
            " " + OWNER_UUID,
            "00000000-0000-0000-0000-000000000000",
        ):
            with self.subTest(bad=bad):
                self.owner["uuid"] = bad
                self.assertIn("UUID_CANONICO_INVALIDO", self.check())
                self.assertEqual(contact_uuid(self.owner), "")
        self.owner["uuid"] = OWNER_UUID
        self.duplicate.pop("uuid")
        self.assertIn("UUID_DUPLICADO_INVALIDO", self.check())

    def test_both_contacts_must_be_present_exactly_once_in_related_contacts(self):
        self.assertIn(
            "CANONICO_AUSENTE_OU_REPETIDO_NOS_RELACIONADOS",
            self.check(related_contacts=[self.duplicate]),
        )
        self.assertIn(
            "DUPLICADO_AUSENTE_OU_REPETIDO_NOS_RELACIONADOS",
            self.check(related_contacts=[self.owner]),
        )
        self.assertIn(
            "DUPLICADO_AUSENTE_OU_REPETIDO_NOS_RELACIONADOS",
            self.check(related_contacts=[self.owner, self.duplicate, self.duplicate]),
        )

    def test_related_contact_with_invalid_uuid_blocks_even_with_matching_phone(self):
        bad = deepcopy(self.owner)
        bad["uuid"] = None
        self.assertIn(
            "UUID_RELACIONADO_INVALIDO",
            self.check(related_contacts=[self.owner, self.duplicate, bad]),
        )

    def test_same_uuid_cannot_be_deleted_as_its_own_duplicate(self):
        self.duplicate["uuid"] = OWNER_UUID
        self.assertIn("MESMO_UUID", self.check())

    def test_duplicate_refusal_cannot_be_lost_in_deletion(self):
        self.duplicate["fields"]["marketing_opt_in"] = {"value": "Recusado"}
        self.assertIn("RECUSA_MARKETING_NAO_PRESERVADA", self.check())
        self.owner["fields"]["marketing_opt_in"] = "recusado"
        self.assertEqual(self.check(), [])

    def test_lgpd_refusal_group_overrides_an_active_field(self):
        self.duplicate["fields"]["opt_in_lgpd"] = "ativo"
        self.duplicate["groups"] = [{"name": "Opt-Out LGPD"}]
        self.owner["fields"]["opt_in_lgpd"] = "ativo"
        self.assertIn("RECUSA_LGPD_NAO_PRESERVADA", self.check())

    def test_uuid_only_opt_out_groups_are_recognized_when_resolved(self):
        self.duplicate["groups"] = [{"uuid": OTHER_UUID}]
        self.assertIn(
            "RECUSA_MARKETING_NAO_PRESERVADA",
            self.check(opt_out_marketing_uuid=OTHER_UUID),
        )

    def test_blocked_and_stopped_cannot_be_silently_lost(self):
        self.duplicate.update(blocked=True, stopped=True)
        self.assertIn("BLOQUEIO_NAO_PRESERVADO", self.check())
        self.assertIn("PARADA_NAO_PRESERVADA", self.check())
        self.owner.update(blocked=True, stopped=True)
        self.assertEqual(self.check(), [])

    def test_positive_consents_are_not_copied_or_inferred(self):
        self.duplicate["fields"].update(marketing_opt_in="ativo", opt_in_lgpd="ativo")
        self.duplicate["groups"] = [
            {"name": "Opt-In LGPD"},
            {"name": "Opt-In Marketing"},
        ]
        before = deepcopy((self.owner, self.duplicate))
        self.assertEqual(contact_refusals(self.duplicate)["reasons"], [])
        self.assertEqual(self.check(), [])
        self.assertEqual((self.owner, self.duplicate), before)
        self.assertNotIn("marketing_opt_in", self.owner["fields"])

    def test_false_negative_fields_and_explicit_false_blocked_are_distinguished(self):
        flags = contact_refusals(
            {
                "blocked": "false",
                "stopped": False,
                "fields": {"opt_in_lgpd": False, "marketing_opt_in": "NÃO"},
            }
        )
        self.assertTrue(flags["lgpd_refused"])
        self.assertTrue(flags["marketing_refused"])
        self.assertFalse(flags["blocked"])
        self.assertFalse(flags["stopped"])

    def test_empty_or_unknown_consent_does_not_count_as_an_explicit_refusal(self):
        for value in (None, "", "não informado", "desconhecido"):
            with self.subTest(value=value):
                self.assertFalse(
                    contact_refusals({"fields": {"opt_in_lgpd": value}})["lgpd_refused"]
                )


if __name__ == "__main__":
    unittest.main()
