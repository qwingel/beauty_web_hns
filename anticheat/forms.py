from django import forms


class SessionCommentForm(forms.Form):
    body = forms.CharField(
        label="Комментарий",
        max_length=2000,
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "Написать комментарий"}),
    )
