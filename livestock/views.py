from django.core.mail import send_mail
from django.shortcuts import get_object_or_404
from django.db.models import Count
from django.db.models.functions import TruncMonth
from datetime import datetime, timedelta
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied
from .views_insights import AMUInsightsViewSet
from .views_insights import FeedInsightsViewSet, YieldInsightsViewSet

from .models import (
    Farm,
    Labourer,
    Livestock,
    HealthRecord,
    AMURecord,
    FeedRecord,
    YieldRecord,
    Drug,
    Feed,
)
from .permissions import IsFarmOwner, IsFarmMember
from .serializers import (
    FarmSerializer,
    LabourerSerializer,
    LivestockSerializer,
    HealthRecordSerializer,
    AMURecordSerializer,
    FeedRecordSerializer,
    YieldRecordSerializer,
    DrugSerializer,
    FeedSerializer,
)

import requests
import os


class FarmViewSet(viewsets.ModelViewSet):
    serializer_class = FarmSerializer

    def get_queryset(self):
        if self.request.user.is_authenticated:
            return Farm.objects.all()
        return Farm.objects.none()

    def get_permissions(self):
        if self.action == "list":
            permission_classes = [IsAuthenticated]
        else:
            permission_classes = [IsAuthenticated, IsFarmOwner]
        return [permission() for permission in permission_classes]

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)


class LabourerViewSet(viewsets.ModelViewSet):
    serializer_class = LabourerSerializer

    def get_queryset(self):
        user = self.request.user
        if hasattr(user, "owned_farm"):
            return Labourer.objects.filter(farm=user.owned_farm)
        elif hasattr(user, "labourer_profile"):
            return Labourer.objects.filter(user=user)
        return Labourer.objects.none()

    def get_permissions(self):
        if self.action == "create":
            return [IsAuthenticated()]
        return [IsAuthenticated(), IsFarmMember()]

    def perform_create(self, serializer):
        if hasattr(self.request.user, "labourer_profile"):
            raise PermissionDenied("You already have a labourer profile.")
        serializer.save(user=self.request.user, status="pending", farm=None)

    @action(detail=True, methods=["post"], permission_classes=[IsAuthenticated])
    def join_farm(self, request, pk=None):
        farm = get_object_or_404(Farm, pk=pk)

        try:
            labourer = Labourer.objects.get(user=request.user)
        except Labourer.DoesNotExist:
            return Response(
                {"detail": "Labourer profile not found."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if labourer.farm is not None:
            if labourer.farm == farm and labourer.status == "pending":
                return Response(
                    {"detail": "You have already sent a request to join this farm."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            elif labourer.farm == farm and labourer.status == "approved":
                return Response(
                    {"detail": "You are already an approved member of this farm."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            elif labourer.farm != farm and labourer.status == "pending":
                return Response(
                    {
                        "detail": "You have a pending request for another farm. Please wait for approval or rejection."
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            elif labourer.farm != farm and labourer.status == "approved":
                return Response(
                    {"detail": "You are already an approved member of another farm."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        labourer.farm = farm
        labourer.status = "pending"
        labourer.save()

        send_mail(
            "New Labourer Request",
            f'{request.user.username} wants to join your farm, "{farm.name}". Go to your dashboard to approve or reject.',
            "noreply@farm.com",
            [farm.owner.email],
            fail_silently=False,
        )

        return Response(
            {"detail": "Request to join farm sent."}, status=status.HTTP_200_OK
        )

    @action(
        detail=True,
        methods=["post"],
        permission_classes=[IsFarmOwner],
        url_path="approve",
    )
    def approve_labourer(self, request, pk=None):
        labourer = self.get_object()
        if labourer.farm.owner != request.user:
            return Response(
                {"detail": "You are not the owner of this farm."},
                status=status.HTTP_403_FORBIDDEN,
            )

        labourer.status = "approved"
        labourer.save()

        send_mail(
            "Farm Join Request Approved",
            f'Your request to join the farm "{labourer.farm.name}" has been approved.',
            "noreply@farm.com",
            [labourer.user.email],
            fail_silently=False,
        )

        return Response({"detail": "Labourer approved."}, status=status.HTTP_200_OK)

    @action(
        detail=True,
        methods=["post"],
        permission_classes=[IsFarmOwner],
        url_path="reject",
    )
    def reject_labourer(self, request, pk=None):
        labourer = self.get_object()
        if labourer.farm.owner != request.user:
            return Response(
                {"detail": "You are not the owner of this farm."},
                status=status.HTTP_403_FORBIDDEN,
            )

        labourer.status = "rejected"
        labourer.save()
        return Response({"detail": "Labourer rejected."}, status=status.HTTP_200_OK)


class DrugViewSet(viewsets.ModelViewSet):
    serializer_class = DrugSerializer
    permission_classes = [IsAuthenticated, IsFarmOwner]  # Only owners can manage drugs

    def get_queryset(self):
        return Drug.objects.all()


class FeedViewSet(viewsets.ModelViewSet):
    serializer_class = FeedSerializer
    permission_classes = [IsAuthenticated, IsFarmOwner]

    def get_queryset(self):
        return Feed.objects.all()


class AMUInsightsViewSet(viewsets.ViewSet):
    permission_classes = [
        IsAuthenticated,
        IsFarmOwner,
    ]

    @action(detail=False, methods=["GET"], url_path="chart-data")
    def chart_data(self, request):
        livestock_id = request.query_params.get("livestock_id")
        if not livestock_id:
            return Response(
                {"error": "livestock_id is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            livestock = Livestock.objects.get(pk=livestock_id, farm__owner=request.user)
        except Livestock.DoesNotExist:
            return Response(
                {"detail": "Livestock not found or you don't own it."},
                status=status.HTTP_404_NOT_FOUND,
            )

        end_date = datetime.now()
        start_date = end_date - timedelta(days=365)

        amu_records = AMURecord.objects.filter(
            health_record__livestock_id=livestock_id,
            health_record__event_date__gte=start_date,
            health_record__event_date__lte=end_date,
        )

        monthly_usage = (
            amu_records.annotate(month=TruncMonth("health_record__event_date"))
            .values("month", "drug__name")
            .annotate(count=Count("id"))
            .order_by("month", "drug__name")
        )

        chart_data = {"labels": [], "datasets": []}

        drugs = Drug.objects.filter(
            id__in=amu_records.values_list("drug_id", flat=True)
        ).distinct()
        drug_datasets = {drug.name: [] for drug in drugs}

        months = []
        current = start_date
        while current <= end_date:
            month_str = current.strftime("%Y-%m")
            months.append(month_str)
            for drug_data in drug_datasets.values():
                drug_data.append(0)
            current = (current.replace(day=1) + timedelta(days=32)).replace(day=1)

        for record in monthly_usage:
            month_str = record["month"].strftime("%Y-%m")
            if month_str in months:
                month_index = months.index(month_str)
                drug_name = record["drug__name"]
                if drug_name in drug_datasets:
                    drug_datasets[drug_name][month_index] = record["count"]

        formatted_labels = [
            datetime.strptime(m, "%Y-%m").strftime("%b %Y") for m in months
        ]
        chart_data["labels"] = formatted_labels

        colors = [
            "#FF6384",
            "#36A2EB",
            "#FFCE56",
            "#4BC0C0",
            "#9966FF",
            "#FF9F40",
            "#FF6384",
            "#C9CBCF",
            "#4BC0C0",
            "#FF9F40",
        ]

        for i, (drug_name, counts) in enumerate(drug_datasets.items()):
            dataset = {
                "label": drug_name,
                "data": counts,
                "backgroundColor": colors[i % len(colors)],
                "borderColor": colors[i % len(colors)],
                "borderWidth": 1,
                "fill": False,
            }
            chart_data["datasets"].append(dataset)

        return Response(
            {
                "chart_data": chart_data,
                "summary": {
                    "total_treatments": amu_records.count(),
                    "unique_drugs": len(drugs),
                    "time_period": f"{formatted_labels[0]} to {formatted_labels[-1]}",
                },
            }
        )

    @action(detail=False, methods=["post"], url_path="generate")
    def generate_insights(self, request):
        livestock_id = request.data.get("livestock_id")

        if not livestock_id:
            return Response(
                {"detail": "livestock_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            livestock = Livestock.objects.get(pk=livestock_id, farm__owner=request.user)
        except Livestock.DoesNotExist:
            return Response(
                {"detail": "Livestock not found or you don't own it."},
                status=status.HTTP_404_NOT_FOUND,
            )

        amu_records = AMURecord.objects.filter(
            health_record__livestock=livestock
        ).order_by("-health_record__event_date")[:5]
        health_records = HealthRecord.objects.filter(livestock=livestock).order_by(
            "-event_date"
        )[:5]

        livestock_data = LivestockSerializer(livestock).data
        amu_data = AMURecordSerializer(amu_records, many=True).data
        health_data = HealthRecordSerializer(health_records, many=True).data

        prompt = f"""
        Analyze the following data for a livestock animal and provide insights on AMU (Antimicrobial Usage).
        Determine if the current drug dosages are correct based on recommended ranges and animal weight.
        If not correct, suggest adjustments (e.g., bring the dosage up or down by X quantity).
        Also, provide any additional relevant insights about the animal's health and drug usage.

        Livestock Details:
        - Species: {livestock_data.get("species")}
        - Breed: {livestock_data.get("breed")}
        - Gender: {livestock_data.get("gender")}
        - Current Weight (kg): {livestock_data.get("current_weight_kg")}
        - Health Status: {livestock_data.get("health_status")}

        Recent Health Records:
        {health_data}

        Recent AMU Records:
        {amu_data}

        Recommended Drug Information (if available in AMU records):
        """

        for amu_record in amu_records:
            if amu_record.drug:
                drug_info = DrugSerializer(amu_record.drug).data
                prompt += f"""
                - Drug Name: {drug_info.get("name")}
                - Active Ingredient: {drug_info.get("active_ingredient")}
                - Species Target: {drug_info.get("species_target")}
                - Recommended Dosage Min: {drug_info.get("recommended_dosage_min")} {drug_info.get("unit")}
                """

        try:
            api_key = os.environ.get("GEMINI_API_KEY", "").strip()
            # FIXED: Changed model to pinned version 'gemini-1.5-flash-001'
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"

            headers = {
                "Content-Type": "application/json",
            }

            data = {
                "contents": [{"parts": [{"text": prompt}]}],
                "system_instruction": {
                    "parts": [
                        {
                            "text": "You are an expert veterinary assistant specializing in livestock health and antimicrobial usage (AMU) analysis."
                        }
                    ]
                },
                "generationConfig": {
                    "temperature": 0.7,
                    "maxOutputTokens": 1500,
                },
            }

            max_retries = 3
            insights = "No insights generated."

            for attempt in range(max_retries):
                try:
                    response = requests.post(
                        url, json=data, headers=headers, timeout=30
                    )

                    if response.status_code == 200:
                        result = response.json()
                        try:
                            insights = result["candidates"][0]["content"]["parts"][0][
                                "text"
                            ]
                        except (KeyError, IndexError):
                            insights = "AI generated an empty response."
                        break
                    else:
                        if attempt == max_retries - 1:
                            insights = f"Error generating insights: {response.status_code} - {response.text}"
                        else:
                            continue

                except requests.exceptions.RequestException as e:
                    if attempt == max_retries - 1:
                        insights = f"Error generating insights: {str(e)}"
                    else:
                        continue

        except Exception as e:
            insights = f"Error generating insights: {str(e)}"

        return Response({"insights": insights}, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="parse-voice")
    def parse_voice_input(self, request):
        """
        Parse voice transcript using Gemini AI to extract form information
        """
        transcript = request.data.get("transcript", "")
        form_type = request.data.get("form_type", "livestock")
        language = request.data.get("language", "en")

        if not transcript:
            return Response(
                {"error": "Transcript is required"}, status=status.HTTP_400_BAD_REQUEST
            )

        form_fields = self.get_form_fields(form_type)

        prompt = f"""You are an expert livestock management assistant. I need you to parse the following voice transcript and extract {form_type} information in a structured JSON format.
            Voice Transcript: "{transcript}"

            The transcript language code is "{language}". If the transcript is not in English, first translate it to English accurately, then extract the fields.

            Please analyze this transcript and extract the following information:
            {form_fields["description"]}

            Expected fields:
            {form_fields["fields"]}

            Important:
            - Return ONLY a valid JSON object.
            - Do NOT add markdown formatting like ```json.
            
            Example response format:
            {form_fields["example"]}
            """

        try:
            api_key = os.environ.get("GEMINI_API_KEY", "").strip()
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"

            headers = {
                "Content-Type": "application/json",
            }

            data = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.1,
                    "maxOutputTokens": 1000,
                    "responseMimeType": "application/json",
                },
            }

            max_retries = 3
            parsed_data = None

            for attempt in range(max_retries):
                try:
                    response = requests.post(
                        url, json=data, headers=headers, timeout=30
                    )

                    if response.status_code == 200:
                        result = response.json()
                        try:
                            ai_response = result["candidates"][0]["content"]["parts"][
                                0
                            ]["text"].strip()
                        except (KeyError, IndexError):
                            parsed_data = {"error": "AI returned empty response"}
                            break

                        # Try to parse the JSON response
                        try:
                            import json

                            if ai_response.startswith("```json"):
                                ai_response = ai_response.replace(
                                    "```json", ""
                                ).replace("```", "")

                            parsed_data = json.loads(ai_response)
                            break
                        except json.JSONDecodeError:
                            import re

                            json_match = re.search(r"\{.*\}", ai_response, re.DOTALL)
                            if json_match:
                                parsed_data = json.loads(json_match.group())
                                break
                            else:
                                if attempt == max_retries - 1:
                                    parsed_data = {
                                        "error": "Could not parse AI response as JSON"
                                    }
                                continue
                    else:
                        if attempt == max_retries - 1:
                            parsed_data = {
                                "error": f"API error: {response.status_code} - {response.text}"
                            }
                        continue

                except requests.exceptions.RequestException as e:
                    if attempt == max_retries - 1:
                        parsed_data = {"error": f"Request error: {str(e)}"}
                    continue

            if parsed_data is None:
                parsed_data = {"error": "Failed to parse voice input"}

        except Exception as e:
            parsed_data = {"error": f"Error processing voice input: {str(e)}"}

        return Response(parsed_data, status=status.HTTP_200_OK)

    def get_form_fields(self, form_type):
        """Get form-specific field definitions for AI parsing"""
        form_definitions = {
            "livestock": {
                "description": """
- tag_id: Animal identification tag/ID
- species: Animal species (cow, buffalo, goat, sheep, chicken, pig)
- breed: Animal breed name
- gender: Gender (M for male, F for female)
- health_status: Health status (healthy, sick, recovering)
- current_weight_kg: Weight in kilograms
- date_of_birth: Birth date in YYYY-MM-DD format
                """,
                "fields": """
- tag_id: String (required) - Animal identification
- species: String (required) - Animal species
- breed: String (required) - Animal breed
- gender: String (required) - M or F
- health_status: String (required) - healthy, sick, or recovering
- current_weight_kg: Decimal (optional) - Weight in kg
- date_of_birth: Date (required) - Birth date
                """,
                "example": '{\n  "tag_id": "COW-001",\n  "species": "cow",\n  "breed": "Holstein",\n  "gender": "F",\n  "health_status": "healthy",\n  "current_weight_kg": 500,\n  "date_of_birth": "2023-01-15"\n}',
            },
            "health": {
                "description": """
- livestock: Livestock ID or tag
- event_type: Event type (vaccination, sickness, check-up, treatment)
- event_date: Event date in YYYY-MM-DD format
- notes: Additional notes
- diagnosis: Medical diagnosis
- treatment_outcome: Treatment outcome (recovered, ongoing, completed)
                """,
                "fields": """
- livestock: String (required) - Livestock ID
- event_type: String (required) - vaccination, sickness, check-up, treatment
- event_date: Date (required) - Event date
- notes: String (optional) - Additional notes
- diagnosis: String (optional) - Medical diagnosis
- treatment_outcome: String (optional) - Treatment outcome
                """,
                "example": '{\n  "livestock": "COW-001",\n  "event_type": "vaccination",\n  "event_date": "2024-01-15",\n  "notes": "Routine vaccination",\n  "diagnosis": "Preventive care",\n  "treatment_outcome": "completed"\n}',
            },
            "feed_record": {
                "description": """
- livestock: Livestock ID or tag
- feed_type: Type of feed (hay, grass, silage, concentrate, grain, etc.)
- feed: Feed name or brand
- quantity_kg: Quantity in kilograms
- price_per_kg: Price per kilogram
- date: Feeding date in YYYY-MM-DD format
                """,
                "fields": """
- livestock: String (required) - Livestock ID
- feed_type: String (required) - Feed type
- feed: String (required) - Feed name
- quantity_kg: Decimal (required) - Quantity in kg
- price_per_kg: Decimal (required) - Price per kg
- date: Date (required) - Feeding date
                """,
                "example": '{\n  "livestock": "COW-001",\n  "feed_type": "hay",\n  "feed": "Alfalfa hay",\n  "quantity_kg": 10.0,\n  "price_per_kg": 50.0,\n  "date": "2024-01-15"\n}',
            },
            "yield_record": {
                "description": """
- livestock: Livestock ID or tag (e.g., COW-123)
- yield_type: Type of yield (milk, eggs, wool, meat, etc.)
- quantity: Yield quantity (numeric)
- unit: Unit of measure (e.g., liters, kg, pieces)
- quality_grade: Quality grade (A, B, C) (optional)
- date: Yield date (accept natural formats like 9 26 2023 or YYYY-MM-DD)
- notes: Additional notes (optional)
                """,
                "fields": """
- livestock: String (required) - Livestock ID
- yield_type: String (required) - Yield type
- quantity: Decimal (required) - Yield quantity
- unit: String (required) - Unit of measure (liters, kg, pieces)
- quality_grade: String (optional) - Quality grade
- date: Date (required) - Yield date in YYYY-MM-DD
- notes: String (optional) - Additional notes
                """,
                "example": '{\n  "livestock": "COW-123",\n  "yield_type": "milk",\n  "quantity": 15,\n  "unit": "liters",\n  "quality_grade": "A",\n  "date": "2023-09-26",\n  "notes": "Morning milking"\n}',
            },
            "drug": {
                "description": """
- name: Drug name
- active_ingredient: Active ingredient
- unit: Measurement unit (e.g., ml, mg, tablet)
- notes: Additional notes (e.g., manufacturer, remarks)
                """,
                "fields": """
- name: String (required) - Drug name
- active_ingredient: String (required) - Active ingredient
- unit: String (optional) - Measurement unit
- notes: String (optional) - Notes or manufacturer details
                """,
                "example": '{\n  "name": "Penicillin",\n  "active_ingredient": "Penicillin",\n  "unit": "ml",\n  "notes": "Manufactured by ABC Pharma"\n}',
            },
            "feed": {
                "description": """
- name: Feed name
- cost_per_kg: Price per kilogram
- notes: Additional notes or remarks (optional)
                """,
                "fields": """
- name: String (required) - Feed name
- cost_per_kg: Decimal (required) - Price per kg
- notes: String (optional) - Additional notes or remarks
                """,
                "example": '{\n  "name": "Sunflower seeds",\n  "cost_per_kg": 500.0,\n  "notes": "Do not buy them; they are expensive."\n}',
            },
        }

        return form_definitions.get(form_type, form_definitions["livestock"])


class LivestockViewSet(viewsets.ModelViewSet):
    serializer_class = LivestockSerializer
    permission_classes = [IsAuthenticated, IsFarmMember]

    def get_queryset(self):
        user = self.request.user
        if hasattr(user, "owned_farm"):
            return Livestock.objects.filter(farm=user.owned_farm)
        elif hasattr(user, "labourer_profile") and user.labourer_profile.farm:
            return Livestock.objects.filter(farm=user.labourer_profile.farm)
        return Livestock.objects.none()

    def perform_create(self, serializer):
        user = self.request.user
        if not hasattr(user, "owned_farm"):
            raise PermissionDenied("Only farm owners can add livestock.")
        serializer.save(farm=user.owned_farm)


class HealthRecordViewSet(viewsets.ModelViewSet):
    serializer_class = HealthRecordSerializer
    permission_classes = [IsAuthenticated, IsFarmMember]

    def get_queryset(self):
        user = self.request.user
        if hasattr(user, "owned_farm"):
            return HealthRecord.objects.filter(livestock__farm=user.owned_farm)
        elif hasattr(user, "labourer_profile") and user.labourer_profile.farm:
            return HealthRecord.objects.filter(
                livestock__farm=user.labourer_profile.farm
            )
        return HealthRecord.objects.none()


class AMURecordViewSet(viewsets.ModelViewSet):
    serializer_class = AMURecordSerializer
    permission_classes = [IsAuthenticated, IsFarmMember]

    def get_queryset(self):
        user = self.request.user
        if hasattr(user, "owned_farm"):
            return AMURecord.objects.filter(
                health_record__livestock__farm=user.owned_farm
            )
        elif hasattr(user, "labourer_profile") and user.labourer_profile.farm:
            return AMURecord.objects.filter(
                health_record__livestock__farm=user.labourer_profile.farm
            )
        return AMURecord.objects.none()


class FeedRecordViewSet(viewsets.ModelViewSet):
    serializer_class = FeedRecordSerializer
    permission_classes = [IsAuthenticated, IsFarmMember]

    def get_queryset(self):
        user = self.request.user
        if hasattr(user, "owned_farm"):
            return FeedRecord.objects.filter(livestock__farm=user.owned_farm)
        elif hasattr(user, "labourer_profile") and user.labourer_profile.farm:
            return FeedRecord.objects.filter(livestock__farm=user.labourer_profile.farm)
        return FeedRecord.objects.none()


class YieldRecordViewSet(viewsets.ModelViewSet):
    serializer_class = YieldRecordSerializer
    permission_classes = [IsAuthenticated, IsFarmMember]

    def get_queryset(self):
        user = self.request.user
        if hasattr(user, "owned_farm"):
            return YieldRecord.objects.filter(livestock__farm=user.owned_farm)
        elif hasattr(user, "labourer_profile") and user.labourer_profile.farm:
            return YieldRecord.objects.filter(
                livestock__farm=user.labourer_profile.farm
            )
        return YieldRecord.objects.none()
