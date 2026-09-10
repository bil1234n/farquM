"""
Tests for the yard: materials, recipes, and the batches that join them.

    python manage.py test production

WHAT THESE PROTECT
------------------
1. The ledger invariant. SUM(movements) == quantity_in_stock, after every
   kind of event, or the store card is fiction.
2. Atomicity. A run that runs out of sand on the last line must not have
   consumed the cement on the first.
3. The cost chain. Materials -> batch cost -> unit cost -> product cost price,
   with rejects carrying their share, because a yard that costs only its good
   blocks believes it is more profitable than it is.
4. Reversal, not deletion. Both halves stay on record, and goods that have
   already been sold cannot be un-made.
5. Scope. One shared store - cement is cement, whoever recorded the
   delivery - while who may ADJUST or REVERSE it stays a permission.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.utils import timezone

from accounts.models import User
from accounts.roles import ensure_system_roles
from inventory.models import MovementType, Product, StockMovement
from production import services
from production.models import (
    MaterialMovement,
    MaterialMovementType,
    ProductionRun,
    ProductionStatus,
    RawMaterial,
    Recipe,
    RecipeItem,
)


def D(value) -> Decimal:
    return Decimal(str(value))


class YardTestBase(TestCase):
    """A block yard: two managers, a product each, and a store of materials."""

    @classmethod
    def setUpTestData(cls):
        ensure_system_roles()
        cls.admin = User.objects.create_user("owner", password="pw", role="ADMIN")
        cls.manager = User.objects.create_user("mary", password="pw", role="MANAGER")
        cls.rival = User.objects.create_user("rob", password="pw", role="MANAGER")
        cls.sales = User.objects.create_user(
            "sam", password="pw", role="SALES", manager=cls.manager
        )

        cls.block = Product.objects.create(
            name="Hollow Block 20cm",
            sku="HB20",
            selling_price=D("32.00"),
            cost_price=D("0.00"),
            stock_quantity=0,
            owner=cls.manager,
        )
        cls.cement = RawMaterial.objects.create(
            name="Cement", code="CEM", unit="KG", unit_cost=D("18.00"),
            reorder_level=D("100"), owner=cls.manager,
        )
        cls.sand = RawMaterial.objects.create(
            name="Sand", code="SAND", unit="M3", unit_cost=D("900.00"),
            reorder_level=D("2"), owner=cls.manager,
        )

        # One mix makes 60 blocks from 50 kg of cement and 0.18 m3 of sand.
        cls.recipe = Recipe.objects.create(product=cls.block, output_quantity=60)
        RecipeItem.objects.create(recipe=cls.recipe, material=cls.cement,
                                  quantity=D("50"))
        RecipeItem.objects.create(recipe=cls.recipe, material=cls.sand,
                                  quantity=D("0.180"))

    def setUp(self):
        # Fresh store for every test, through the service so the ledger is real.
        services.receive_material(self.cement, D("1000"), user=self.manager,
                                  unit_cost=D("18.00"))
        services.receive_material(self.sand, D("10"), user=self.manager,
                                  unit_cost=D("900.00"))

    def as_(self, user) -> Client:
        client = Client()
        client.force_login(user)
        return client

    def assertLedgerAgrees(self, material):
        material.refresh_from_db()
        total = sum(
            (m.quantity_delta for m in material.movements.all()), Decimal("0.000")
        )
        self.assertEqual(
            total,
            material.quantity_in_stock,
            f"{material.code}: ledger sums to {total} but the card says "
            f"{material.quantity_in_stock}",
        )


class MaterialLedgerTests(YardTestBase):
    def test_a_delivery_adds_and_records(self):
        before = self.cement.quantity_in_stock
        movement = services.receive_material(
            self.cement, D("250"), user=self.manager, unit_cost=D("19.50"),
            reference="INV-8891",
        )
        self.cement.refresh_from_db()
        self.assertEqual(self.cement.quantity_in_stock, before + D("250"))
        self.assertEqual(movement.movement_type, MaterialMovementType.PURCHASE)
        self.assertEqual(movement.reference, "INV-8891")
        self.assertLedgerAgrees(self.cement)

    def test_a_delivery_updates_what_the_material_costs(self):
        services.receive_material(self.cement, D("100"), user=self.manager,
                                  unit_cost=D("21.00"))
        self.cement.refresh_from_db()
        self.assertEqual(self.cement.unit_cost, D("21.00"))

    def test_fractional_quantities_survive_the_round_trip(self):
        """The reason materials are not Products: 0.185 is not an integer."""
        services.receive_material(self.sand, D("0.185"), user=self.manager)
        self.sand.refresh_from_db()
        self.assertEqual(self.sand.quantity_in_stock, D("10.185"))
        self.assertLedgerAgrees(self.sand)

    def test_consuming_more_than_exists_is_refused(self):
        with self.assertRaises(services.InsufficientMaterialError):
            services.apply_material_movement(
                self.sand, D("-999"), MaterialMovementType.CONSUMED,
                user=self.manager,
            )
        self.assertLedgerAgrees(self.sand)

    def test_a_zero_movement_is_refused(self):
        with self.assertRaises(ValidationError):
            services.apply_material_movement(
                self.cement, 0, MaterialMovementType.ADJUSTMENT, user=self.manager
            )

    def test_a_recount_writes_only_the_difference(self):
        services.recount_material(self.cement, D("940"), user=self.manager,
                                  reason="Monthly count")
        self.cement.refresh_from_db()
        self.assertEqual(self.cement.quantity_in_stock, D("940"))
        last = self.cement.movements.first()
        self.assertEqual(last.movement_type, MaterialMovementType.ADJUSTMENT)
        self.assertEqual(last.quantity_delta, D("-60"))
        self.assertLedgerAgrees(self.cement)

    def test_a_recount_that_matches_writes_nothing(self):
        movement = services.recount_material(
            self.cement, self.cement.quantity_in_stock, user=self.manager
        )
        self.assertIsNone(movement)

    def test_waste_may_go_below_zero_but_a_delivery_may_not_be_negative(self):
        services.waste_material(self.sand, D("0.5"), user=self.manager,
                                reason="Washed away")
        self.assertLedgerAgrees(self.sand)
        with self.assertRaises(ValidationError):
            services.receive_material(self.sand, D("-1"), user=self.manager)

    def test_movements_cannot_be_deleted(self):
        movement = self.cement.movements.first()
        with self.assertRaises(PermissionError):
            movement.delete()

    def test_the_code_is_generated_across_the_whole_store(self):
        """
        One yard, one sequence. Two tins of red oxide bought by two managers
        sit on the same shelf, so they must not both be called RO-001.
        """
        mine = RawMaterial.objects.create(name="Red Oxide", owner=self.manager)
        theirs = RawMaterial.objects.create(name="Red Oxide", owner=self.rival)
        self.assertEqual(mine.code, "RO-001")
        self.assertEqual(theirs.code, "RO-002")

    def test_low_and_empty_are_computed_from_the_reorder_level(self):
        services.recount_material(self.cement, D("50"), user=self.manager)
        self.cement.refresh_from_db()
        self.assertTrue(self.cement.is_low)
        self.assertEqual(self.cement.stock_status, "LOW")

        services.recount_material(self.cement, D("0"), user=self.manager)
        self.cement.refresh_from_db()
        self.assertTrue(self.cement.is_empty)
        self.assertEqual(self.cement.stock_status, "OUT")

    def test_reconciliation_reports_drift(self):
        # Write behind the service's back, exactly as a stray .update() would.
        RawMaterial.objects.filter(pk=self.cement.pk).update(
            quantity_in_stock=D("5")
        )
        drift = services.reconcile_all_materials()
        self.assertIn(self.cement.code, drift)
        self.assertLedgerAgrees(self.cement)


class PlanningTests(YardTestBase):
    def test_the_recipe_scales_to_the_batch(self):
        plan = services.plan_for(self.block, 120)  # two mixes
        self.assertTrue(plan["has_recipe"])
        by_name = {line["material_name"]: line for line in plan["lines"]}
        self.assertEqual(by_name["Cement"]["required"], D("100.000"))
        self.assertEqual(by_name["Sand"]["required"], D("0.360"))
        # 100 kg x 18 + 0.36 m3 x 900 = 1800 + 324
        self.assertEqual(plan["material_cost"], D("2124.00"))
        self.assertEqual(plan["unit_cost"], D("17.70"))
        self.assertTrue(plan["can_produce"])

    def test_a_part_mix_scales_too(self):
        plan = services.plan_for(self.block, 30)  # half a mix
        by_name = {line["material_name"]: line for line in plan["lines"]}
        self.assertEqual(by_name["Cement"]["required"], D("25.000"))

    def test_a_shortage_is_named_before_anything_is_written(self):
        services.recount_material(self.sand, D("0.1"), user=self.manager)
        plan = services.plan_for(self.block, 120)
        self.assertFalse(plan["can_produce"])
        self.assertIn("Sand", plan["shortages"])
        short = next(l for l in plan["lines"] if l["material_name"] == "Sand")
        self.assertTrue(short["is_short"])
        self.assertEqual(short["short_by"], D("0.260"))

    def test_a_product_with_no_recipe_plans_an_empty_list(self):
        plain = Product.objects.create(name="Bagged Sand", sku="BS1",
                                       selling_price=D("50"), owner=self.manager)
        plan = services.plan_for(plain, 10)
        self.assertFalse(plan["has_recipe"])
        self.assertEqual(plan["lines"], [])
        self.assertTrue(plan["can_produce"])

    def test_planning_zero_is_refused(self):
        with self.assertRaises(ValidationError):
            services.plan_for(self.block, 0)


class ProductionTests(YardTestBase):
    def _run(self, produced=60, rejected=0, cement=D("50"), sand=D("0.180")):
        return services.record_production(
            product=self.block,
            quantity_produced=produced,
            quantity_rejected=rejected,
            materials=[
                {"material": self.cement, "quantity": cement,
                 "expected_quantity": D("50")},
                {"material": self.sand, "quantity": sand,
                 "expected_quantity": D("0.180")},
            ],
            user=self.manager,
        )

    def test_a_run_moves_both_sides_of_the_yard(self):
        run = self._run()

        self.cement.refresh_from_db()
        self.sand.refresh_from_db()
        self.block.refresh_from_db()

        self.assertEqual(self.cement.quantity_in_stock, D("950"))
        self.assertEqual(self.sand.quantity_in_stock, D("9.820"))
        self.assertEqual(self.block.stock_quantity, 60)
        self.assertLedgerAgrees(self.cement)
        self.assertLedgerAgrees(self.sand)

        # And the finished goods arrived through the product ledger, marked as
        # made rather than bought.
        movement = StockMovement.objects.filter(product=self.block).first()
        self.assertEqual(movement.movement_type, MovementType.PRODUCTION)
        self.assertEqual(movement.reference, run.reference)

    def test_the_batch_is_costed_and_the_product_cost_follows(self):
        run = self._run()
        # 50 x 18 + 0.18 x 900 = 900 + 162
        self.assertEqual(run.material_cost, D("1062.00"))
        self.assertEqual(run.unit_cost, D("17.70"))
        self.block.refresh_from_db()
        self.assertEqual(self.block.cost_price, D("17.70"))

    def test_the_product_cost_can_be_left_alone(self):
        self.block.cost_price = D("25.00")
        self.block.save(update_fields=["cost_price"])
        services.record_production(
            product=self.block,
            quantity_produced=60,
            materials=[{"material": self.cement, "quantity": D("50")}],
            user=self.manager,
            update_product_cost=False,
        )
        self.block.refresh_from_db()
        self.assertEqual(self.block.cost_price, D("25.00"))

    def test_rejects_reduce_the_yield_and_carry_their_share_of_the_cost(self):
        run = self._run(produced=54, rejected=6)
        self.assertEqual(run.total_attempted, 60)
        self.assertEqual(run.yield_percent, D("90.00"))
        # A tenth of the batch failed, so a tenth of 1062 was spent on nothing.
        self.assertEqual(run.rejected_cost, D("106.20"))
        # And the good blocks carry the whole cost, which is the honest figure.
        self.assertEqual(run.unit_cost, D("19.67"))
        self.block.refresh_from_db()
        self.assertEqual(self.block.stock_quantity, 54)

    def test_the_actual_quantity_is_stored_next_to_the_expected_one(self):
        run = self._run(cement=D("54"))
        line = run.materials.get(material=self.cement)
        self.assertEqual(line.quantity, D("54.000"))
        self.assertEqual(line.expected_quantity, D("50.000"))
        self.assertEqual(line.variance, D("4.000"))

    def test_a_short_material_aborts_the_whole_run(self):
        """The cement on line one must not vanish because line two failed."""
        cement_before = self.cement.quantity_in_stock
        blocks_before = self.block.stock_quantity

        with self.assertRaises(services.InsufficientMaterialError):
            services.record_production(
                product=self.block,
                quantity_produced=60,
                materials=[
                    {"material": self.cement, "quantity": D("50")},
                    {"material": self.sand, "quantity": D("999")},
                ],
                user=self.manager,
            )

        self.cement.refresh_from_db()
        self.block.refresh_from_db()
        self.assertEqual(self.cement.quantity_in_stock, cement_before)
        self.assertEqual(self.block.stock_quantity, blocks_before)
        self.assertFalse(ProductionRun.objects.exists())
        self.assertLedgerAgrees(self.cement)

    def test_a_run_needs_a_quantity_and_at_least_one_material(self):
        with self.assertRaises(ValidationError):
            services.record_production(
                product=self.block, quantity_produced=0,
                materials=[{"material": self.cement, "quantity": D("1")}],
                user=self.manager,
            )
        with self.assertRaises(ValidationError):
            services.record_production(
                product=self.block, quantity_produced=10, materials=[],
                user=self.manager,
            )

    def test_the_same_material_twice_is_refused(self):
        with self.assertRaises(ValidationError):
            services.record_production(
                product=self.block,
                quantity_produced=60,
                materials=[
                    {"material": self.cement, "quantity": D("25")},
                    {"material": self.cement, "quantity": D("25")},
                ],
                user=self.manager,
            )

    def test_references_are_sequential_and_unique(self):
        first = self._run()
        second = self._run()
        self.assertTrue(first.reference.startswith("PRD-"))
        self.assertNotEqual(first.reference, second.reference)


class IsolationTests(YardTestBase):
    def test_any_material_in_the_store_can_be_consumed(self):
        """
        Cement is cement. Whoever recorded the delivery, a batch that uses it
        takes it off the one store card - which is what makes that card mean
        anything.
        """
        theirs = RawMaterial.objects.create(
            name="Their Cement", code="TC", unit="KG", unit_cost=D("18"),
            owner=self.rival,
        )
        services.receive_material(theirs, D("500"), user=self.rival)

        services.record_production(
            product=self.block,
            quantity_produced=10,
            materials=[{"material": theirs, "quantity": D("10")}],
            user=self.manager,
        )
        theirs.refresh_from_db()
        self.assertEqual(theirs.quantity_in_stock, D("490.000"))
        self.assertLedgerAgrees(theirs)

    def test_a_batch_can_be_produced_into_any_product_on_the_shelf(self):
        theirs = Product.objects.create(
            name="Their Block", sku="TB1", selling_price=D("30"), owner=self.rival
        )
        run = services.record_production(
            product=theirs,
            quantity_produced=10,
            materials=[{"material": self.cement, "quantity": D("10")}],
            user=self.manager,
        )
        theirs.refresh_from_db()
        self.assertEqual(theirs.stock_quantity, 10)
        self.assertEqual(run.quantity_produced, 10)

    def test_a_run_belongs_to_whoever_owns_the_shelf_it_filled(self):
        """
        Recorded by the sales user under their manager's product: the batch
        must land in the manager's book, not the sales user's, or the blocks
        and the batch that made them end up on opposite sides of every filter.
        """
        self.sales.extra_permissions = ["production.create"]
        self.sales.save(update_fields=["extra_permissions"])
        run = services.record_production(
            product=self.block,
            quantity_produced=10,
            materials=[{"material": self.cement, "quantity": D("10")}],
            user=self.sales,
        )
        self.assertEqual(run.owner, self.manager)
        self.assertEqual(run.created_by, self.sales)


class ReversalTests(YardTestBase):
    def setUp(self):
        super().setUp()
        self.run = services.record_production(
            product=self.block,
            quantity_produced=60,
            materials=[
                {"material": self.cement, "quantity": D("50")},
                {"material": self.sand, "quantity": D("0.180")},
            ],
            user=self.manager,
        )

    def test_reversal_puts_the_materials_back_and_takes_the_goods_off(self):
        services.reverse_production(self.run, user=self.admin, reason="Wrong mix")

        self.cement.refresh_from_db()
        self.sand.refresh_from_db()
        self.block.refresh_from_db()
        self.run.refresh_from_db()

        self.assertEqual(self.cement.quantity_in_stock, D("1000.000"))
        self.assertEqual(self.sand.quantity_in_stock, D("10.000"))
        self.assertEqual(self.block.stock_quantity, 0)
        self.assertEqual(self.run.status, ProductionStatus.REVERSED)
        self.assertEqual(self.run.reversed_by, self.admin)
        self.assertLedgerAgrees(self.cement)

    def test_both_halves_stay_on_record(self):
        services.reverse_production(self.run, user=self.admin, reason="Wrong mix")
        kinds = list(
            self.cement.movements.values_list("movement_type", flat=True)
        )
        self.assertIn(MaterialMovementType.CONSUMED, kinds)
        self.assertIn(MaterialMovementType.PRODUCTION_REVERSAL, kinds)
        # Nothing was deleted: delivery, consumption and return are all there.
        self.assertEqual(len(kinds), 3)

    def test_a_reason_is_required(self):
        with self.assertRaises(ValidationError):
            services.reverse_production(self.run, user=self.admin, reason="   ")

    def test_a_run_cannot_be_reversed_twice(self):
        services.reverse_production(self.run, user=self.admin, reason="Wrong mix")
        with self.assertRaises(ValidationError):
            services.reverse_production(self.run, user=self.admin, reason="Again")

    def test_goods_already_sold_cannot_be_un_made(self):
        """
        The blocks left the yard. Returning the cement now would invent
        material the business does not have.
        """
        from inventory.services import deduct_for_sale

        deduct_for_sale(self.block, 60, user=self.manager, reference="TXN-1")
        with self.assertRaises(ValidationError):
            services.reverse_production(self.run, user=self.admin, reason="Oops")

        self.cement.refresh_from_db()
        self.assertEqual(self.cement.quantity_in_stock, D("950.000"))
        self.assertLedgerAgrees(self.cement)


class WebAccessTests(YardTestBase):
    """Every page checks its own permission, whatever the sidebar shows."""

    def test_a_sales_user_cannot_open_the_yard(self):
        # A refusal redirects to the shared forbidden page, which answers 403.
        # Following it is what a browser does, so that is what is asserted.
        client = self.as_(self.sales)
        for url in ("/production/materials/", "/production/runs/",
                    "/production/recipes/"):
            self.assertEqual(
                client.get(url, follow=True).status_code, 403, url
            )

    def test_a_manager_can_run_the_yard(self):
        client = self.as_(self.manager)
        for url in ("/production/materials/", "/production/runs/",
                    "/production/recipes/", "/production/runs/new/",
                    "/production/materials/low/"):
            self.assertEqual(client.get(url).status_code, 200, url)

    def test_a_manager_may_not_reverse_a_run(self):
        run = services.record_production(
            product=self.block, quantity_produced=10,
            materials=[{"material": self.cement, "quantity": D("10")}],
            user=self.manager,
        )
        response = self.as_(self.manager).post(
            f"/production/runs/{run.pk}/reverse/", {"reason": "no"}, follow=True
        )
        self.assertEqual(response.status_code, 403)
        run.refresh_from_db()
        self.assertEqual(run.status, ProductionStatus.COMPLETED)

    def test_the_store_page_shows_the_whole_yard(self):
        RawMaterial.objects.create(name="Rival Lime", code="RL", owner=self.rival)
        page = self.as_(self.manager).get("/production/materials/")
        self.assertContains(page, "Cement")
        self.assertContains(page, "Rival Lime")

    def test_recording_a_run_through_the_form(self):
        client = self.as_(self.manager)
        response = client.post(
            "/production/runs/new/",
            {
                "product": self.block.pk,
                "quantity_produced": 60,
                "quantity_rejected": 0,
                "produced_on": timezone.localdate().isoformat(),
                "notes": "Morning shift",
                # Parallel arrays, the same shape the till posts its cart in.
                "material_id[]": [str(self.cement.pk), str(self.sand.pk)],
                "quantity[]": ["50", "0.180"],
                "expected[]": ["50", "0.180"],
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ProductionRun.objects.count(), 1)
        self.block.refresh_from_db()
        self.assertEqual(self.block.stock_quantity, 60)


class ApiTests(YardTestBase):
    def test_the_material_list_is_one_shared_store(self):
        """
        There is one yard. A material another manager bought is in the same
        bay, so it appears in the same list - otherwise two managers order
        cement twice and neither can explain the stock figure.
        """
        RawMaterial.objects.create(name="Rival Lime", code="RL", owner=self.rival)
        rows = self.as_(self.manager).get("/api/materials/").json()["results"]
        names = {row["name"] for row in rows}
        self.assertIn("Cement", names)
        self.assertIn("Rival Lime", names)

    def test_a_sales_user_is_refused(self):
        self.assertEqual(
            self.as_(self.sales).get("/api/materials/").status_code, 403
        )

    def test_receiving_a_delivery_through_the_api(self):
        response = self.as_(self.manager).post(
            f"/api/materials/{self.cement.pk}/receive/",
            {"quantity": "250.5", "unit_cost": "19.00", "reference": "INV-2"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.cement.refresh_from_db()
        self.assertEqual(self.cement.quantity_in_stock, D("1250.500"))
        self.assertEqual(self.cement.unit_cost, D("19.00"))

    def test_the_plan_endpoint_answers_before_anything_moves(self):
        response = self.as_(self.manager).get(
            f"/api/production/plan/?product={self.block.pk}&quantity=120"
        )
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertTrue(body["has_recipe"])
        self.assertEqual(body["material_cost"], "2124.00")
        self.assertEqual(len(body["lines"]), 2)
        self.cement.refresh_from_db()
        self.assertEqual(self.cement.quantity_in_stock, D("1000.000"))

    def test_recording_a_run_through_the_api(self):
        response = self.as_(self.manager).post(
            "/api/production/",
            {
                "product": self.block.pk,
                "quantity_produced": 60,
                "quantity_rejected": 4,
                "materials": [
                    {"material": self.cement.pk, "quantity": "50",
                     "expected_quantity": "50"},
                    {"material": self.sand.pk, "quantity": "0.180"},
                ],
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(body["quantity_produced"], 60)
        self.assertEqual(len(body["materials"]), 2)
        self.block.refresh_from_db()
        self.assertEqual(self.block.stock_quantity, 60)

    def test_a_shortage_comes_back_as_a_sentence_not_a_500(self):
        response = self.as_(self.manager).post(
            "/api/production/",
            {
                "product": self.block.pk,
                "quantity_produced": 60,
                "materials": [{"material": self.sand.pk, "quantity": "9999"}],
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("Sand", response.content.decode())

    def test_reversal_needs_its_own_permission(self):
        run = services.record_production(
            product=self.block, quantity_produced=10,
            materials=[{"material": self.cement, "quantity": D("10")}],
            user=self.manager,
        )
        refused = self.as_(self.manager).post(
            f"/api/production/{run.pk}/reverse/", {"reason": "wrong"},
            content_type="application/json",
        )
        self.assertEqual(refused.status_code, 403)

        allowed = self.as_(self.admin).post(
            f"/api/production/{run.pk}/reverse/", {"reason": "wrong mix"},
            content_type="application/json",
        )
        self.assertEqual(allowed.status_code, 200, allowed.content)
        run.refresh_from_db()
        self.assertEqual(run.status, ProductionStatus.REVERSED)

    def test_the_recipe_can_be_written_in_one_request(self):
        other = Product.objects.create(
            name="Solid Block", sku="SB1", selling_price=D("40"), owner=self.manager
        )
        response = self.as_(self.manager).put(
            f"/api/recipes/{other.pk}/",
            {
                "output_quantity": 40,
                "items": [{"material": self.cement.pk, "quantity": "45"}],
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        other.refresh_from_db()
        self.assertEqual(other.recipe.output_quantity, 40)
        self.assertEqual(other.recipe.items.count(), 1)

    def test_material_cost_is_hidden_from_someone_who_may_not_see_costs(self):
        self.sales.extra_permissions = ["material.view"]
        self.sales.save(update_fields=["extra_permissions"])
        rows = self.as_(self.sales).get("/api/materials/").json()["results"]
        self.assertTrue(rows)
        self.assertNotIn("unit_cost", rows[0])
        self.assertNotIn("stock_value", rows[0])

    def test_the_movement_list_reads_back_the_store_card(self):
        services.waste_material(self.cement, D("3"), user=self.manager,
                                reason="Burst bag")
        rows = self.as_(self.manager).get(
            f"/api/materials/{self.cement.pk}/movements/"
        ).json()
        self.assertGreaterEqual(len(rows), 2)
        self.assertEqual(rows[0]["movement_type"], MaterialMovementType.WASTE)

    def test_used_in_names_the_products_a_material_goes_into(self):
        rows = self.as_(self.manager).get(
            f"/api/materials/{self.cement.pk}/used-in/"
        ).json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["product_name"], self.block.name)
        self.assertEqual(D(rows[0]["quantity"]), D("50.000"))
        self.assertEqual(rows[0]["output_quantity"], 60)

    def test_used_in_is_store_knowledge_not_recipe_authorship(self):
        """
        Someone who may see the store but not edit recipes still gets this.
        A storeman about to write cement off should be able to see what he is
        about to starve.
        """
        self.sales.extra_permissions = ["material.view"]
        self.sales.save(update_fields=["extra_permissions"])
        client = self.as_(self.sales)

        self.assertEqual(
            client.get(f"/api/recipes/{self.block.pk}/").status_code, 403
        )
        # ...but the material's own page still answers.
        allowed = client.get(f"/api/materials/{self.cement.pk}/used-in/")
        self.assertEqual(allowed.status_code, 200, allowed.content)

    def test_used_in_answers_for_any_material_in_the_store(self):
        other = RawMaterial.objects.create(
            name="Rival Lime", code="RL2", owner=self.rival
        )
        allowed = self.as_(self.manager).get(f"/api/materials/{other.pk}/used-in/")
        self.assertEqual(allowed.status_code, 200, allowed.content)

    def test_a_sales_user_is_still_refused_the_store_entirely(self):
        """
        Widening the store to the whole business did not hand it to everyone
        in the business. `material.view` is still not in the Sales role.
        """
        self.assertEqual(
            self.as_(self.sales).get("/api/materials/").status_code, 403
        )


class YardLanguageTests(YardTestBase):
    """The server's own words come back translated; the yard's do not."""

    def test_movement_and_unit_labels_come_back_in_amharic(self):
        from api.messages import EXACT_AM

        rows = self.as_(self.manager).get(
            f"/api/materials/{self.cement.pk}/movements/",
            HTTP_ACCEPT_LANGUAGE="am",
        ).json()
        self.assertEqual(
            rows[0]["movement_type_display"], EXACT_AM["Delivery received"]
        )
        self.assertEqual(rows[0]["unit_display"], EXACT_AM["Kilogram"])

    def test_a_material_actually_called_bag_is_left_alone(self):
        """
        The counterpart of the customer called "Paid". A material's name is
        something somebody typed, and the renderer must never touch it even
        when it happens to collide with a word in the table.
        """
        RawMaterial.objects.create(name="Bag", code="BAGX", owner=self.manager)
        rows = self.as_(self.manager).get(
            "/api/materials/", HTTP_ACCEPT_LANGUAGE="am"
        ).json()["results"]
        self.assertIn("Bag", {row["name"] for row in rows})

    def test_a_shortage_is_explained_in_amharic(self):
        response = self.as_(self.manager).post(
            "/api/production/",
            {
                "product": self.block.pk,
                "quantity_produced": 10,
                "materials": [{"material": self.sand.pk, "quantity": "9999"}],
            },
            content_type="application/json",
            HTTP_ACCEPT_LANGUAGE="am",
        )
        self.assertEqual(response.status_code, 400)
        body = response.content.decode()
        self.assertNotIn("Not enough", body)
        # The material's own name survives inside the translated sentence.
        self.assertIn("Sand", body)

    def test_the_run_status_label_is_translated(self):
        from api.messages import EXACT_AM

        run = services.record_production(
            product=self.block, quantity_produced=10,
            materials=[{"material": self.cement, "quantity": D("10")}],
            user=self.manager,
        )
        row = self.as_(self.manager).get(
            f"/api/production/{run.pk}/", HTTP_ACCEPT_LANGUAGE="am"
        ).json()
        self.assertEqual(row["status_display"], EXACT_AM["Completed"])
        self.assertEqual(row["reference"], run.reference)
