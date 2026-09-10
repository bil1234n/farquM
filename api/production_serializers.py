"""
Serializers for the yard: materials, their ledger, recipes and runs.

Kept out of api/serializers.py because that file is already the longest in the
project, and production is a self-contained area that a reader can take in on
its own.
"""
from decimal import Decimal

from rest_framework import serializers

from production.models import (
    MaterialMovement,
    ProductionMaterial,
    ProductionRun,
    RawMaterial,
    Recipe,
    RecipeItem,
)

from .serializers import FinancialFieldsMixin, OwnerNameMixin


class RawMaterialSerializer(
    OwnerNameMixin, FinancialFieldsMixin, serializers.ModelSerializer
):
    # Cost is not something a yard hand needs to read off a phone in front of
    # a customer, and the same rule already governs product cost.
    financial_fields = ("unit_cost", "stock_value")

    unit_display = serializers.CharField(source="get_unit_display", read_only=True)
    supplier_name = serializers.CharField(
        source="supplier.name", default=None, read_only=True
    )
    stock_value = serializers.DecimalField(
        max_digits=18, decimal_places=2, read_only=True
    )
    stock_status = serializers.CharField(read_only=True)
    stock_status_label = serializers.CharField(read_only=True)
    owner_name = serializers.SerializerMethodField()

    # Optional for the same reason a product SKU is: the model builds one from
    # the name, and a form that says "leave blank" must not be answered with
    # "this field is required".
    code = serializers.CharField(max_length=40, required=False, allow_blank=True)

    class Meta:
        model = RawMaterial
        fields = [
            "id", "code", "name", "description",
            "unit", "unit_display",
            "supplier", "supplier_name",
            "quantity_in_stock", "reorder_level", "unit_cost", "stock_value",
            "stock_status", "stock_status_label",
            "allow_negative_stock", "is_active", "owner_name",
        ]
        # Quantity moves only through the ledger. Letting a PATCH set it would
        # be exactly the drift the ledger exists to prevent.
        read_only_fields = ["id", "quantity_in_stock"]

    def validate_code(self, value):
        value = (value or "").strip()
        if not value:
            return value
        if self.instance is not None:
            owner_id = self.instance.owner_id
        else:
            request = self.context.get("request")
            owner_id = getattr(getattr(request, "user", None), "pk", None)

        clash = RawMaterial.objects.alive().filter(
            owner_id=owner_id, code__iexact=value
        )
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        first = clash.first()
        if first is not None:
            raise serializers.ValidationError(
                f"You already have a material with this code: '{first.name}'."
            )
        return value

    def validate(self, attrs):
        # A blank code on an edit means "leave it alone", never "generate a
        # new one" - the old code may be written on a bin.
        if self.instance is not None and not (attrs.get("code") or "").strip():
            attrs.pop("code", None)
        return super().validate(attrs)


class MaterialMovementSerializer(serializers.ModelSerializer):
    material_name = serializers.CharField(source="material.name", read_only=True)
    material_code = serializers.CharField(source="material.code", read_only=True)
    unit_display = serializers.CharField(
        source="material.get_unit_display", read_only=True
    )
    movement_type_display = serializers.CharField(
        source="get_movement_type_display", read_only=True
    )
    performed_by_name = serializers.CharField(
        source="performed_by.display_name", default=None, read_only=True
    )

    class Meta:
        model = MaterialMovement
        fields = [
            "id", "material", "material_name", "material_code", "unit_display",
            "movement_type", "movement_type_display",
            "quantity_delta", "quantity_before", "quantity_after",
            "reference", "reason", "performed_by_name", "created_at",
        ]


class MaterialReceiveSerializer(serializers.Serializer):
    quantity = serializers.DecimalField(
        max_digits=14, decimal_places=3, min_value=Decimal("0.001")
    )
    unit_cost = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True,
        min_value=Decimal("0"),
    )
    reference = serializers.CharField(max_length=60, required=False, allow_blank=True)
    reason = serializers.CharField(max_length=255, required=False, allow_blank=True)


class MaterialAdjustSerializer(serializers.Serializer):
    """
    Waste, a supplier return, or a counted figure.

    `kind` is a closed list on purpose. PURCHASE and CONSUMED are written by
    their own services with their own rules, and letting a client name any
    movement type would be a way to invent a delivery that nobody paid for.
    """

    KINDS = (
        ("WASTE", "Spoiled / written off"),
        ("RETURN_OUT", "Returned to supplier"),
        ("RECOUNT", "Counted on the shelf"),
    )

    kind = serializers.ChoiceField(choices=KINDS)
    quantity = serializers.DecimalField(
        max_digits=14, decimal_places=3, min_value=Decimal("0")
    )
    reason = serializers.CharField(max_length=255, required=False, allow_blank=True)

    def validate(self, attrs):
        # Zero is a real statement for a count and meaningless for a write-off.
        if attrs["kind"] != "RECOUNT" and attrs["quantity"] <= 0:
            raise serializers.ValidationError(
                {"quantity": "Enter a quantity greater than zero."}
            )
        return attrs


# ---------------------------------------------------------------------------
# Recipes
# ---------------------------------------------------------------------------
class RecipeItemSerializer(serializers.ModelSerializer):
    material_name = serializers.CharField(source="material.name", read_only=True)
    material_code = serializers.CharField(source="material.code", read_only=True)
    unit_display = serializers.CharField(
        source="material.get_unit_display", read_only=True
    )
    available = serializers.DecimalField(
        source="material.quantity_in_stock", max_digits=14, decimal_places=3,
        read_only=True,
    )

    class Meta:
        model = RecipeItem
        fields = [
            "id", "material", "material_name", "material_code", "unit_display",
            "quantity", "available",
        ]


class RecipeSerializer(FinancialFieldsMixin, serializers.ModelSerializer):
    financial_fields = ("material_cost", "cost_per_unit")

    items = RecipeItemSerializer(many=True)
    product_name = serializers.CharField(source="product.name", read_only=True)
    material_cost = serializers.DecimalField(
        max_digits=14, decimal_places=2, read_only=True
    )
    cost_per_unit = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True
    )

    class Meta:
        model = Recipe
        fields = [
            "id", "product", "product_name", "output_quantity", "notes",
            "is_active", "items", "material_cost", "cost_per_unit",
        ]
        read_only_fields = ["id", "product"]

    def validate_output_quantity(self, value):
        if value < 1:
            raise serializers.ValidationError("A batch must make at least one unit.")
        return value

    def validate_items(self, value):
        seen = set()
        for item in value:
            material = item["material"]
            if material.pk in seen:
                raise serializers.ValidationError(
                    f"'{material.name}' is listed twice. Combine it into one line."
                )
            seen.add(material.pk)
            if item["quantity"] <= 0:
                raise serializers.ValidationError(
                    f"How much '{material.name}' does one batch use?"
                )
        return value

    def _write_items(self, recipe, items):
        # Replaced wholesale rather than matched row by row. A recipe is short
        # and rarely edited, and reconciling ids for six lines is a lot of
        # machinery to get subtly wrong.
        recipe.items.all().delete()
        RecipeItem.objects.bulk_create(
            [
                RecipeItem(
                    recipe=recipe,
                    material=item["material"],
                    quantity=item["quantity"],
                )
                for item in items
            ]
        )

    def create(self, validated_data):
        items = validated_data.pop("items", [])
        recipe = Recipe.objects.create(**validated_data)
        self._write_items(recipe, items)
        return recipe

    def update(self, instance, validated_data):
        items = validated_data.pop("items", None)
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save()
        if items is not None:
            self._write_items(instance, items)
        return instance


# ---------------------------------------------------------------------------
# Production
# ---------------------------------------------------------------------------
class ProductionMaterialSerializer(FinancialFieldsMixin,
                                   serializers.ModelSerializer):
    financial_fields = ("unit_cost", "line_cost")

    material_name = serializers.CharField(source="material.name", read_only=True)
    material_code = serializers.CharField(source="material.code", read_only=True)
    unit_display = serializers.CharField(
        source="material.get_unit_display", read_only=True
    )
    line_cost = serializers.DecimalField(
        max_digits=14, decimal_places=2, read_only=True
    )
    variance = serializers.DecimalField(
        max_digits=14, decimal_places=3, read_only=True
    )

    class Meta:
        model = ProductionMaterial
        fields = [
            "id", "material", "material_name", "material_code", "unit_display",
            "quantity", "expected_quantity", "variance", "unit_cost", "line_cost",
        ]


class ProductionLineWriteSerializer(serializers.Serializer):
    material = serializers.PrimaryKeyRelatedField(
        queryset=RawMaterial.objects.alive()
    )
    quantity = serializers.DecimalField(
        max_digits=14, decimal_places=3, min_value=Decimal("0.001")
    )
    expected_quantity = serializers.DecimalField(
        max_digits=14, decimal_places=3, required=False, allow_null=True
    )


class ProductionRunSerializer(
    OwnerNameMixin, FinancialFieldsMixin, serializers.ModelSerializer
):
    financial_fields = ("material_cost", "unit_cost", "rejected_cost")

    product_name = serializers.CharField(source="product.name", read_only=True)
    product_sku = serializers.CharField(source="product.sku", read_only=True)
    unit_display = serializers.CharField(
        source="product.get_unit_display", read_only=True
    )
    status_display = serializers.CharField(
        source="get_status_display", read_only=True
    )
    materials = ProductionMaterialSerializer(many=True, read_only=True)
    yield_percent = serializers.DecimalField(
        max_digits=6, decimal_places=2, read_only=True
    )
    rejected_cost = serializers.DecimalField(
        max_digits=14, decimal_places=2, read_only=True
    )
    total_attempted = serializers.IntegerField(read_only=True)
    created_by_name = serializers.CharField(
        source="created_by.display_name", default=None, read_only=True
    )
    reversed_by_name = serializers.CharField(
        source="reversed_by.display_name", default=None, read_only=True
    )
    owner_name = serializers.SerializerMethodField()

    class Meta:
        model = ProductionRun
        fields = [
            "id", "reference", "product", "product_name", "product_sku",
            "unit_display",
            "quantity_produced", "quantity_rejected", "total_attempted",
            "yield_percent", "produced_on",
            "material_cost", "unit_cost", "rejected_cost",
            "status", "status_display", "notes",
            "reversed_at", "reversed_by_name", "reversal_reason",
            "materials", "created_by_name", "owner_name", "created_at",
        ]
        read_only_fields = fields


class ProductionRunCreateSerializer(serializers.Serializer):
    """
    What the phone and the web form both send.

    Deliberately a plain Serializer rather than a ModelSerializer: creating a
    run is not "write these columns", it is a service call that moves stock in
    two ledgers. Letting DRF build the row would skip all of it.
    """

    product = serializers.IntegerField()
    quantity_produced = serializers.IntegerField(min_value=1)
    quantity_rejected = serializers.IntegerField(min_value=0, required=False,
                                                 default=0)
    produced_on = serializers.DateField(required=False, allow_null=True)
    notes = serializers.CharField(max_length=2000, required=False,
                                  allow_blank=True)
    materials = ProductionLineWriteSerializer(many=True)
    update_product_cost = serializers.BooleanField(required=False, default=True)

    def validate_materials(self, value):
        if not value:
            raise serializers.ValidationError(
                "Record at least one material used in this run."
            )
        return value


class ReversalSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=255)

    def validate_reason(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError(
                "Give a reason for reversing this run."
            )
        return value
