# {{ personal.first_name }} {{ personal.last_name }}

{{ personal.location }} | {{ personal.email }} | {{ personal.phone }}
{% if personal.linkedin_url %}[LinkedIn]({{ personal.linkedin_url }}){% endif %}{% if personal.github_url %} | [GitHub]({{ personal.github_url }}){% endif %}{% if personal.portfolio_url %} | [Portfolio]({{ personal.portfolio_url }}){% endif %}

---

## Professional Summary

{{ tailored_summary }}

---

## Experience

{% for job in experience %}
### {{ job.title }}
**{{ job.company }}** | {{ job.location }} | {{ job.start_date }} – {{ job.end_date }}

{{ job.description }}

{% for achievement in job.achievements %}
- {{ achievement }}
{% endfor %}

*Technologies: {{ job.technologies | join(', ') }}*

{% endfor %}

---

## Education

{% for edu in education %}
### {{ edu.degree }}
**{{ edu.institution }}**{% if edu.graduation_date %} | {{ edu.graduation_date }}{% endif %}{% if edu.gpa %} | GPA: {{ edu.gpa }}{% endif %}

{% endfor %}

---

## Skills

**Programming Languages:** {{ skills.programming_languages | map(attribute='name') | join(', ') }}

**Frameworks & Tools:** {{ skills.frameworks_and_tools | join(', ') }}

{% if skills.certifications %}
**Certifications:** {% for cert in skills.certifications %}{{ cert.name }} ({{ cert.date }}){% if not loop.last %}, {% endif %}{% endfor %}
{% endif %}
