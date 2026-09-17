"""
The managed pick-lists, for both front doors.

One endpoint serves the phone and the web forms alike - the web templates call
it with the session cookie, the app with its token - so "Dashen Bank" typed
once on a phone appears in the browser's dropdown a moment later. Two separate
implementations would guarantee the opposite.

WHY ANY SIGNED-IN USER MAY ADD ONE
----------------------------------
The whole point of adding from inside a select is that the person filling the
form is not blocked by a list somebody else forgot to maintain. Gating it on a
permission recreates exactly the free-typing it replaces: the clerk writes
"dashn" in the notes and moves on.

Removal is narrower - see Option.may_be_removed_by. You may take back your own
mistake; clearing out somebody else's entry is a shared-list decision.
"""
from django.db.models import Count, Q
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action, api_view
from rest_framework.response import Response

from core.models import Option
from core.options import GROUPS, group_for, is_known


class OptionSerializer(serializers.ModelSerializer):
    can_remove = serializers.SerializerMethodField()
    group_label = serializers.CharField(read_only=True)

    class Meta:
        model = Option
        fields = [
            "id", "group", "group_label", "label", "sort_order",
            "is_active", "is_seeded", "use_count", "can_remove",
        ]
        read_only_fields = ["id", "is_seeded", "use_count"]

    def get_can_remove(self, obj) -> bool:
        request = self.context.get("request")
        return obj.may_be_removed_by(getattr(request, "user", None))

    def validate_group(self, value):
        value = (value or "").strip().upper()
        if not is_known(value):
            raise serializers.ValidationError("That list does not exist.")
        return value

    def validate_label(self, value):
        value = " ".join((value or "").split())
        if not value:
            raise serializers.ValidationError("Type the name you want to add.")
        if len(value) > 120:
            raise serializers.ValidationError("That is too long for a list entry.")
        return value


class OptionViewSet(viewsets.ModelViewSet):
    """
    /api/options/?group=BANK

    `group` is required on a list: there is no screen that wants every entry
    of every list at once, and returning them all would make the client filter
    a payload it should never have been sent.
    """

    serializer_class = OptionSerializer
    # No permission_map: this is deliberately open to any signed-in user.
    # DRF's default IsAuthenticated (config/settings.py) is the whole rule.
    pagination_class = None

    def get_queryset(self):
        qs = Option.objects.all()
        params = self.request.query_params
        group = (params.get("group") or "").strip().upper()
        if group:
            qs = qs.in_group(group)
        elif self.action == "list":
            # An empty result rather than the whole table, so a client that
            # forgets the parameter fails loudly instead of quietly loading
            # every list in the system.
            return qs.none()

        if params.get("all") != "true":
            qs = qs.active()

        q = (params.get("q") or "").strip()
        if q:
            qs = qs.filter(label__icontains=q)

        # Most-used first inside the manual sort order, so the three banks a
        # yard actually uses rise to the top of a list of thirty.
        return qs.order_by("sort_order", "-use_count", "label")

    def perform_create(self, serializer):
        """
        Add an entry, or revive one that was taken out before.

        Re-adding a label that already exists is not an error - it is somebody
        who could not see it because it was inactive, and refusing them would
        be a dead end with no way out from the form they are standing in.
        """
        group = serializer.validated_data["group"]
        label = serializer.validated_data["label"]

        existing = Option.objects.in_group(group).filter(label__iexact=label).first()
        if existing is not None:
            if not existing.is_active:
                existing.is_active = True
                existing.save(update_fields=["is_active", "updated_at"])
            serializer.instance = existing
            return

        serializer.save(created_by=self.request.user, is_seeded=False)

    def perform_update(self, serializer):
        instance = serializer.instance
        if not instance.may_be_removed_by(self.request.user):
            raise serializers.ValidationError(
                "You can only change entries you added yourself."
            )
        serializer.save()

    def destroy(self, request, *args, **kwargs):
        """
        Take an entry out of the list.

        Records that already name it keep their snapshot of the label, so this
        never rewrites history - see core.models.Option. A seeded entry is
        deactivated rather than deleted so the next deployment's seed does not
        simply put it back.
        """
        option = self.get_object()
        if not option.may_be_removed_by(request.user):
            return Response(
                {"detail": "You can only remove entries you added yourself."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if option.is_seeded:
            option.is_active = False
            option.save(update_fields=["is_active", "updated_at"])
            return Response(status=status.HTTP_204_NO_CONTENT)
        option.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=["get"])
    def groups(self, request):
        """Every list this build knows about, for a settings screen."""
        counts = {
            row["group"]: row["n"]
            for row in Option.objects.active().values("group").annotate(
                n=Count("id")
            )
        }
        return Response([
            {
                "key": g.key,
                "label": g.label,
                "add_label": g.add_label,
                "help": g.help,
                "count": counts.get(g.key, 0),
            }
            for g in GROUPS
        ])


@api_view(["GET"])
def option_bundle(request):
    """
    Several lists in one round trip.

    /api/options/bundle/?groups=BANK,MOBILE_MONEY

    The sale screen needs the bank list and the wallet list before the seller
    has decided which one applies, and a phone on a yard's connection should
    not pay two round trips to find out.
    """
    wanted = [
        key.strip().upper()
        for key in (request.query_params.get("groups") or "").split(",")
        if key.strip()
    ]
    wanted = [key for key in wanted if is_known(key)]
    if not wanted:
        return Response({})

    rows = (
        Option.objects.active()
        .filter(Q(group__in=wanted))
        .order_by("sort_order", "-use_count", "label")
    )
    serializer = OptionSerializer(rows, many=True, context={"request": request})

    bundle: dict[str, list] = {key: [] for key in wanted}
    for item in serializer.data:
        bundle.setdefault(item["group"], []).append(item)

    return Response({
        "groups": {
            key: {
                "label": group_for(key).label,
                "add_label": group_for(key).add_label,
                "help": group_for(key).help,
            }
            for key in wanted
        },
        "options": bundle,
    })
